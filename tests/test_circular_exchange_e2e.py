import pytest
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_CHIEF_WARDEN,
    ROLE_DEAN,
    ROLE_HOSTEL_ADMIN,
    ROLE_STUDENT,
    ROLE_SYSADMIN,
    ROLE_WARDEN,
    create_user_with_role,
    ensure_roles_exist,
)
from apps.core.models import Institution
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)
from apps.exchange.selectors import can_user_decide_proposal
from apps.exchange.services import (
    approve_and_execute_vacant_transfer,
    create_exchange_request,
    decide_exchange_proposal,
    execute_exchange_transfer,
    expire_single_proposal,
    respond_to_proposal,
    run_institution_matching,
)
from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
    WardenAssignment,
)
from apps.hostels.services import assign_student_to_bed


def _configure_all_exchange_policies(institution, **kwargs):
    """Ensure all exchange policies are explicitly configured."""
    defaults = {
        PolicyRule.Key.COOLING_OFF_DAYS: 0,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
        PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS: 48,
        PolicyRule.Key.MAX_CYCLE_LENGTH: 5,
    }
    defaults.update(kwargs)
    for key, val in defaults.items():
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


def _setup_circular_hostel_env(
    cycle_length,
    institution,
    warden_user,
    prefix="circ",
    resident_category=User.ResidentialEligibility.MALE,
):
    """Create hostel infrastructure and cycle_length students assigned to distinct beds."""
    ensure_roles_exist()
    hostel = Hostel.objects.create(
        institution=institution,
        name=f"Hostel {prefix.upper()}",
        code=f"H-{prefix[:3].upper()}",
        resident_category=resident_category,
    )
    WardenAssignment.objects.get_or_create(
        warden=warden_user,
        hostel=hostel,
        defaults={"is_primary": True},
    )

    block = Block.objects.create(hostel=hostel, name="Block A", code="BA")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1F")

    students = []
    rooms = []
    beds = []
    occupancies = []

    for i in range(cycle_length):
        room = Room.objects.create(
            floor=floor,
            room_number=f"{100 + i + 1}",
            room_type=Room.RoomType.SINGLE,
            occupancy_category="Standard",
            operational_status=OperationalStatus.IN_SERVICE,
        )
        bed = Bed.objects.create(
            room=room,
            bed_label="A",
            operational_status=OperationalStatus.IN_SERVICE,
        )
        student = create_user_with_role(
            email=f"{prefix}.student{i + 1}@apex.test",
            full_name=f"Student {prefix.capitalize()} {chr(65 + i)}",
            role_name=ROLE_STUDENT,
            institution=institution,
            password="TestPassword123!",
            registration_number=f"REG-{prefix.upper()}-{i + 1:03d}",
            residential_eligibility=resident_category,
        )
        occ = assign_student_to_bed(student=student, bed=bed, created_by=warden_user)

        rooms.append(room)
        beds.append(bed)
        students.append(student)
        occupancies.append(occ)

    return {
        "hostel": hostel,
        "block": block,
        "floor": floor,
        "rooms": rooms,
        "beds": beds,
        "students": students,
        "occupancies": occupancies,
    }


# =========================================================================
# 1. Parameterized End-to-End Cycle Test (Lengths 2, 3, 4, 5)
# =========================================================================

@pytest.mark.django_db
@pytest.mark.parametrize("cycle_length", [2, 3, 4, 5])
def test_complete_exchange_cycle_through_real_request_workflow(
    cycle_length,
    client,
    institution,
    warden_user,
):
    """
    Test complete lifecycle across all 4 supported cycle lengths (2, 3, 4, 5):
    1. Students create requests through the actual UI request-creation workflow (client.post).
    2. Requests are verified: OPEN status, no premature direct proposals, beds not locked.
    3. Matching engine is executed: finds exactly 1 candidate cycle of length N.
    4. Proposal is activated in PENDING_ACCEPTANCE with N participants and correct proposed beds.
    5. Each participant accepts one by one; status advances to PENDING_APPROVAL on final acceptance.
    6. Authorized Warden approves proposal with auto_execute=True.
    7. Atomic bed rotation verified in the database:
       - Every student transferred to their requested bed.
       - Old occupancies closed as TRANSFERRED.
       - Transfer records created.
       - No duplicate bed occupancies.
    """
    _configure_all_exchange_policies(institution)
    env = _setup_circular_hostel_env(
        cycle_length=cycle_length,
        institution=institution,
        warden_user=warden_user,
        prefix=f"c{cycle_length}",
    )
    students = env["students"]
    beds = env["beds"]
    hostel = env["hostel"]

    # -----------------------------------------------------------------
    # Step 1: Create requests through real application workflow
    # -----------------------------------------------------------------
    created_requests = []
    for i in range(cycle_length):
        target_bed = beds[(i + 1) % cycle_length]
        client.force_login(students[i])

        response = client.post(
            reverse("exchange:request_create"),
            data={
                "preferred_bed": target_bed.id,
                "preferred_hostel": hostel.id,
                "reason": f"Academic study group collaboration preference for student {i + 1}",
            },
        )
        assert response.status_code == 302, f"Request creation failed for student {i}: {response.context.get('form').errors if hasattr(response, 'context') and response.context else 'redirect failed'}"
        assert response.url == reverse("exchange:request_list")

        req = ExchangeRequest.objects.get(student=students[i])
        assert req.status == ExchangeRequest.Status.OPEN, f"Request for student {i} should be OPEN, was {req.status}"
        assert req.matching_status == ExchangeRequest.MatchingStatus.WAITING
        assert req.request_type == ExchangeRequest.RequestType.EXCHANGE
        assert req.preferred_bed_id == target_bed.id
        created_requests.append(req)

    # Verify no premature proposals or reservations were created during request creation
    assert ExchangeProposal.objects.filter(institution=institution).count() == 0
    assert ProposalParticipant.objects.filter(is_active_reservation=True).count() == 0

    # -----------------------------------------------------------------
    # Step 2: Run matching engine
    # -----------------------------------------------------------------
    job = run_institution_matching(institution_id=institution.id, trigger_reason="e2e_cycle_test")
    assert job.status == job.Status.COMPLETED
    assert job.candidates_found_count >= 1
    assert job.proposals_activated_count == 1

    # Verify proposal
    proposal = ExchangeProposal.objects.get(institution=institution)
    assert proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    assert proposal.cycle_length == cycle_length

    participants = list(proposal.participants.order_by("sequence_order"))
    assert len(participants) == cycle_length

    # Verify every participant has the correct proposed bed matching their request preference
    proposal_student_ids = {p.student_id for p in participants}
    assert proposal_student_ids == {s.id for s in students}

    for idx, p in enumerate(participants):
        req = p.exchange_request
        assert p.proposed_bed_id == req.preferred_bed_id
        assert p.proposed_bed_id != p.current_occupancy.bed_id
        assert p.response_status == ProposalParticipant.ResponseStatus.PENDING
        assert p.is_active_reservation is True

        # Check cyclic continuity: proposed_bed must be the current bed of the next participant in sequence
        next_p = participants[(idx + 1) % cycle_length]
        assert p.proposed_bed_id == next_p.current_occupancy.bed_id

    # Verify all requests are now MATCHED
    for req in created_requests:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.MATCHED
        assert req.matching_status == ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE

    # -----------------------------------------------------------------
    # Step 3: Multi-Party Acceptance Workflow
    # -----------------------------------------------------------------
    # Accept from student 0 to cycle_length - 2: proposal stays PENDING_ACCEPTANCE
    for i in range(cycle_length - 1):
        part, prop = respond_to_proposal(
            proposal_id=proposal.id,
            student=students[i],
            action="accept",
        )
        assert part.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
        prop.refresh_from_db()
        assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    # Final student accepts: proposal advances to PENDING_APPROVAL
    final_part, final_prop = respond_to_proposal(
        proposal_id=proposal.id,
        student=students[cycle_length - 1],
        action="accept",
    )
    assert final_part.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    final_prop.refresh_from_db()
    assert final_prop.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert final_prop.accepted_at is not None

    # -----------------------------------------------------------------
    # Step 4: Warden Approval & Atomic Bed Rotation Execution
    # -----------------------------------------------------------------
    # Unauthorized student cannot approve
    can_decide, reason = can_user_decide_proposal(students[0], final_prop)
    assert can_decide is False

    # Authorized hostel warden can decide
    can_decide_w, reason_w = can_user_decide_proposal(warden_user, final_prop)
    assert can_decide_w is True

    approval, executed_prop = decide_exchange_proposal(
        proposal_id=proposal.id,
        actor=warden_user,
        decision="approve",
        remarks=f"Approved {cycle_length}-way circular bed exchange.",
        auto_execute=True,
    )
    assert approval.decision == Approval.Decision.APPROVED
    executed_prop.refresh_from_db()
    assert executed_prop.status == ExchangeProposal.Status.COMPLETED

    # -----------------------------------------------------------------
    # Step 5: Verify Atomic Database Rotation
    # -----------------------------------------------------------------
    for i in range(cycle_length):
        student = students[i]
        old_bed = beds[i]
        expected_new_bed = beds[(i + 1) % cycle_length]

        # Verify old occupancy is TRANSFERRED
        old_occ = StudentOccupancy.objects.get(student=student, bed=old_bed)
        assert old_occ.status == StudentOccupancy.OccupancyStatus.TRANSFERRED
        assert old_occ.end_date == timezone.now().date()

        # Verify new occupancy is ACTIVE on expected_new_bed
        new_occ = StudentOccupancy.objects.get(
            student=student,
            bed=expected_new_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        assert new_occ.start_date == timezone.now().date()

        # Verify transfer record exists
        t_rec = TransferRecord.objects.get(student=student, proposal=proposal)
        assert t_rec.previous_bed_id == old_bed.id
        assert t_rec.new_bed_id == expected_new_bed.id

        # Verify request marked COMPLETED
        req = created_requests[i]
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.COMPLETED

    # Verify strictly 1 active student per bed
    for bed in beds:
        active_count = StudentOccupancy.objects.filter(
            bed=bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).count()
        assert active_count == 1, f"Bed {bed.bed_label} should have exactly 1 active occupant, found {active_count}"


# =========================================================================
# 2. Participant Rejection Workflow
# =========================================================================

@pytest.mark.django_db
def test_participant_decline_cancels_proposal_and_reverts_requests(
    client,
    institution,
    warden_user,
):
    """When any participant declines, proposal is cancelled and non-expired requests revert to OPEN."""
    _configure_all_exchange_policies(institution)
    env = _setup_circular_hostel_env(
        cycle_length=3,
        institution=institution,
        warden_user=warden_user,
        prefix="decl",
    )
    students = env["students"]
    beds = env["beds"]

    requests = []
    for i in range(3):
        req = create_exchange_request(
            student=students[i],
            data={"preferred_bed": beds[(i + 1) % 3], "reason": "Cycle request"},
            direct_invitation=False,
        )
        requests.append(req)

    # Match and activate
    job = run_institution_matching(institution_id=institution.id)
    proposal = ExchangeProposal.objects.get(institution=institution)
    assert proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    # Student 0 accepts
    respond_to_proposal(proposal_id=proposal.id, student=students[0], action="accept")

    # Student 1 declines
    part, updated_prop = respond_to_proposal(
        proposal_id=proposal.id,
        student=students[1],
        action="decline",
    )
    assert part.response_status == ProposalParticipant.ResponseStatus.DECLINED
    assert updated_prop.status == ExchangeProposal.Status.CANCELLED

    # Reservations released
    assert not ProposalParticipant.objects.filter(proposal=proposal, is_active_reservation=True).exists()

    # All unexpired requests returned to OPEN
    for req in requests:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.OPEN
        assert req.matching_status == ExchangeRequest.MatchingStatus.WAITING


# =========================================================================
# 3. Proposal Expiry Workflow
# =========================================================================

@pytest.mark.django_db
def test_proposal_expiry_releases_reservations_and_reverts_requests(
    client,
    institution,
    warden_user,
):
    """When a proposal acceptance deadline lapses, expiring it releases workflow reservations and reopens requests."""
    _configure_all_exchange_policies(institution)
    env = _setup_circular_hostel_env(
        cycle_length=3,
        institution=institution,
        warden_user=warden_user,
        prefix="exp",
    )
    students = env["students"]
    beds = env["beds"]

    requests = []
    for i in range(3):
        req = create_exchange_request(
            student=students[i],
            data={"preferred_bed": beds[(i + 1) % 3], "reason": "Cycle request"},
            direct_invitation=False,
        )
        requests.append(req)

    run_institution_matching(institution_id=institution.id)
    proposal = ExchangeProposal.objects.get(institution=institution)

    # Fast forward beyond proposal acceptance deadline
    future_time = timezone.now() + timedelta(hours=72)
    expired = expire_single_proposal(proposal, now=future_time)
    assert expired is True

    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.EXPIRED

    # Reservations released
    assert not ProposalParticipant.objects.filter(proposal=proposal, is_active_reservation=True).exists()

    # Requests reopened
    for req in requests:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.OPEN
        assert req.matching_status == ExchangeRequest.MatchingStatus.WAITING


# =========================================================================
# 4. Cross-Hostel Circular Exchange Workflow
# =========================================================================

@pytest.mark.django_db
def test_cross_hostel_circular_exchange_requires_chief_warden(
    client,
    institution,
    warden_user,
):
    """
    In a cross-hostel cycle:
    1. Proposal is classified as is_cross_hostel == True.
    2. Assigned hostel warden is denied approval authority.
    3. Chief Warden can approve and execute atomic transfer across hostels.
    """
    _configure_all_exchange_policies(institution)
    chief_warden = create_user_with_role(
        email="cw.circular@apex.test",
        full_name="Chief Warden Circular",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )

    # Create 3 distinct hostels
    hostels = []
    beds = []
    students = []
    occupancies = []
    for h_idx in range(3):
        h = Hostel.objects.create(
            institution=institution,
            name=f"Cross Hostel {h_idx + 1}",
            code=f"CH-{h_idx + 1}",
            resident_category=User.ResidentialEligibility.MALE,
        )
        WardenAssignment.objects.create(warden=warden_user, hostel=h, is_primary=True)
        block = Block.objects.create(hostel=h, name="Block A", code=f"BA{h_idx}")
        floor = Floor.objects.create(block=block, floor_number=1, floor_label="1F")
        room = Room.objects.create(floor=floor, room_number="101", operational_status=OperationalStatus.IN_SERVICE)
        bed = Bed.objects.create(room=room, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)

        student = create_user_with_role(
            email=f"cross.student{h_idx + 1}@apex.test",
            full_name=f"Cross Student {h_idx + 1}",
            role_name=ROLE_STUDENT,
            institution=institution,
            password="TestPassword123!",
            registration_number=f"REG-CH-{h_idx + 1}",
            residential_eligibility=User.ResidentialEligibility.MALE,
        )
        occ = assign_student_to_bed(student=student, bed=bed, created_by=warden_user)

        hostels.append(h)
        beds.append(bed)
        students.append(student)
        occupancies.append(occ)

    # Students submit requests in cycle: 0 -> 1, 1 -> 2, 2 -> 0
    for i in range(3):
        create_exchange_request(
            student=students[i],
            data={"preferred_bed": beds[(i + 1) % 3], "reason": "Cross hostel cycle"},
            direct_invitation=False,
        )

    run_institution_matching(institution_id=institution.id)
    proposal = ExchangeProposal.objects.get(institution=institution)
    assert proposal.is_cross_hostel is True
    assert proposal.classification == "cross_hostel"

    # All participants accept
    for s in students:
        respond_to_proposal(proposal_id=proposal.id, student=s, action="accept")

    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert Notification.objects.filter(
        recipient=chief_warden,
        proposal=proposal,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
        read_at__isnull=True,
    ).exists()
    assert not Notification.objects.filter(
        recipient=warden_user,
        proposal=proposal,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
    ).exists()

    # Standard hostel warden cannot decide cross-hostel proposal
    can_decide_w, reason_w = can_user_decide_proposal(warden_user, proposal)
    assert can_decide_w is False
    assert "Chief Warden" in reason_w

    with pytest.raises(PermissionDenied, match="Cross-hostel proposals can only be decided by the Chief Warden"):
        decide_exchange_proposal(proposal_id=proposal.id, actor=warden_user, decision="approve")

    # Chief warden approves
    can_decide_cw, reason_cw = can_user_decide_proposal(chief_warden, proposal)
    assert can_decide_cw is True

    approval, completed_prop = decide_exchange_proposal(
        proposal_id=proposal.id,
        actor=chief_warden,
        decision="approve",
        auto_execute=True,
    )
    assert completed_prop.status == ExchangeProposal.Status.COMPLETED


# =========================================================================
# 5. Direct Invitation Compatibility Workflow
# =========================================================================

@pytest.mark.django_db
def test_direct_invitation_workflow_preserves_compatibility(
    client,
    institution,
    warden_user,
):
    """Direct invitation flow continues working when explicitly invoked or requested via direct_invitation=True."""
    _configure_all_exchange_policies(institution)
    env = _setup_circular_hostel_env(
        cycle_length=2,
        institution=institution,
        warden_user=warden_user,
        prefix="dircompat",
    )
    s1, s2 = env["students"]
    b1, b2 = env["beds"]

    # Student 1 sends direct invitation
    req = create_exchange_request(
        student=s1,
        data={"preferred_bed": b2, "reason": "Direct swap with roommate"},
        direct_invitation=True,
    )
    assert req.status == ExchangeRequest.Status.MATCHED

    proposal = ExchangeProposal.objects.get(institution=institution)
    assert proposal.cycle_length == 2
    assert proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    # Recipient accepts
    respond_to_proposal(proposal_id=proposal.id, student=s2, action="accept")
    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.PENDING_APPROVAL

    # Warden approves
    decide_exchange_proposal(
        proposal_id=proposal.id,
        actor=warden_user,
        decision="approve",
        auto_execute=True,
    )
    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.COMPLETED

    # Bed rotation verified
    assert StudentOccupancy.objects.get(student=s1, status=StudentOccupancy.OccupancyStatus.ACTIVE).bed == b2
    assert StudentOccupancy.objects.get(student=s2, status=StudentOccupancy.OccupancyStatus.ACTIVE).bed == b1


# =========================================================================
# 6. Vacant-Bed Transfer Compatibility Workflow
# =========================================================================

@pytest.mark.django_db
def test_vacant_bed_transfer_workflow_preserves_compatibility(
    client,
    institution,
    warden_user,
):
    """Vacant bed transfer request continues working without regression."""
    _configure_all_exchange_policies(institution)
    env = _setup_circular_hostel_env(
        cycle_length=1,
        institution=institution,
        warden_user=warden_user,
        prefix="vaccompat",
    )
    student = env["students"][0]
    old_bed = env["beds"][0]

    # Create a vacant bed
    room2 = Room.objects.create(
        floor=env["floor"],
        room_number="102",
        operational_status=OperationalStatus.IN_SERVICE,
    )
    vacant_bed = Bed.objects.create(
        room=room2,
        bed_label="B",
        operational_status=OperationalStatus.IN_SERVICE,
    )

    # Student creates request targeting vacant bed
    req = create_exchange_request(
        student=student,
        data={"preferred_bed": vacant_bed, "reason": "Need quieter room"},
        direct_invitation=False,
    )
    assert req.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER
    assert req.status == ExchangeRequest.Status.PENDING_APPROVAL

    # Warden approves vacant transfer
    approval, rec = approve_and_execute_vacant_transfer(
        request_id=req.id,
        actor=warden_user,
        remarks="Vacant transfer approved.",
    )
    assert rec is not None
    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.COMPLETED

    # Old bed is now vacant, new bed is active
    assert StudentOccupancy.objects.get(student=student, status=StudentOccupancy.OccupancyStatus.ACTIVE).bed == vacant_bed
    assert not StudentOccupancy.objects.filter(bed=old_bed, status=StudentOccupancy.OccupancyStatus.ACTIVE).exists()


# =========================================================================
# 7. Concurrency and Failure Rollback Protection
# =========================================================================

@pytest.mark.django_db
def test_execution_failure_rolls_back_entire_cycle(
    client,
    institution,
    warden_user,
):
    """If one bed allocation update fails during execution, all changes roll back and no bed is left in a dirty state."""
    _configure_all_exchange_policies(institution)
    env = _setup_circular_hostel_env(
        cycle_length=3,
        institution=institution,
        warden_user=warden_user,
        prefix="rollback",
    )
    students = env["students"]
    beds = env["beds"]

    for i in range(3):
        create_exchange_request(
            student=students[i],
            data={"preferred_bed": beds[(i + 1) % 3], "reason": "Cycle"},
            direct_invitation=False,
        )

    run_institution_matching(institution_id=institution.id)
    proposal = ExchangeProposal.objects.get(institution=institution)
    for s in students:
        respond_to_proposal(proposal_id=proposal.id, student=s, action="accept")

    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.PENDING_APPROVAL

    # Simulate database error during transfer creation
    with patch("apps.hostels.models.StudentOccupancy.objects.create", side_effect=RuntimeError("Database write error")):
        with pytest.raises(RuntimeError, match="Database write error"):
            decide_exchange_proposal(
                proposal_id=proposal.id,
                actor=warden_user,
                decision="approve",
                auto_execute=True,
            )

    # Verify rollback: proposal remains PENDING_APPROVAL, all original occupancies remain ACTIVE
    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.PENDING_APPROVAL
    for i in range(3):
        occ = StudentOccupancy.objects.get(student=students[i], bed=beds[i])
        assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE
