import pytest
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
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
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)
from apps.exchange.selectors import (
    can_user_decide_proposal,
    get_approvals_queryset,
    get_pending_approvals_count,
)
from apps.exchange.services import (
    activate_direct_occupied_bed_request,
    create_exchange_request,
    decide_exchange_proposal,
    execute_exchange_transfer,
    respond_to_proposal,
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


def _configure_policies(institution):
    defaults = {
        PolicyRule.Key.COOLING_OFF_DAYS: 0,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
    }
    for key, val in defaults.items():
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


def _setup_two_bed_env(institution, warden, prefix="dir"):
    hostel = Hostel.objects.create(
        institution=institution,
        name=f"Hostel {prefix.upper()}",
        code=f"H-{prefix[:3].upper()}",
        resident_category=User.ResidentialEligibility.MALE,
    )
    WardenAssignment.objects.create(warden=warden, hostel=hostel, is_primary=True)
    block = Block.objects.create(hostel=hostel, name="Block A", code="BA")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1F")
    room = Room.objects.create(
        floor=floor,
        room_number="101",
        room_type=Room.RoomType.DOUBLE,
        operational_status=OperationalStatus.IN_SERVICE,
    )
    bed1 = Bed.objects.create(room=room, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=room, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    s1 = create_user_with_role(
        email=f"sender.{prefix}@apex.test",
        full_name=f"Sender {prefix.capitalize()}",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number=f"REG-S-{prefix.upper()}",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    s2 = create_user_with_role(
        email=f"recipient.{prefix}@apex.test",
        full_name=f"Recipient {prefix.capitalize()}",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number=f"REG-R-{prefix.upper()}",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )

    occ1 = assign_student_to_bed(student=s1, bed=bed1, created_by=warden)
    occ2 = assign_student_to_bed(student=s2, bed=bed2, created_by=warden)

    return {
        "hostel": hostel,
        "room": room,
        "bed1": bed1,
        "bed2": bed2,
        "s1": s1,
        "s2": s2,
        "occ1": occ1,
        "occ2": occ2,
    }


# =========================================================================
# 1. Sending to Occupied Bed Creates Pending Invitation Without Recipient Request
# =========================================================================

@pytest.mark.django_db
def test_send_exchange_to_occupied_bed_creates_invitation(institution, warden_user):
    """
    When John selects Rohan's occupied bed:
    - Resolves occupant server-side.
    - Creates John's request and pending invitation in one transaction.
    - Sender is recorded as ACCEPTED.
    - Recipient is recorded as PENDING with exchange_request=None (no fabricated request).
    """
    ensure_roles_exist()
    _configure_policies(institution)
    env = _setup_two_bed_env(institution, warden_user, prefix="direct1")

    # Recipient has ZERO exchange requests
    assert ExchangeRequest.objects.filter(student=env["s2"]).count() == 0

    # Sender creates request targeting recipient's bed (bed2)
    req = create_exchange_request(
        student=env["s1"],
        data={
            "preferred_hostel": env["hostel"],
            "preferred_bed": env["bed2"],
            "reason": "Swap with roommate's bed",
        },
    )

    assert req.request_type == ExchangeRequest.RequestType.EXCHANGE
    assert req.status == ExchangeRequest.Status.MATCHED

    # Recipient still has ZERO exchange requests
    assert ExchangeRequest.objects.filter(student=env["s2"]).count() == 0

    # Proposal is created
    proposal = ExchangeProposal.objects.get(created_by=env["s1"])
    assert proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    assert proposal.cycle_length == 2

    participants = list(proposal.participants.order_by("sequence_order"))
    assert len(participants) == 2

    # Sender participant
    p_sender = participants[0]
    assert p_sender.student == env["s1"]
    assert p_sender.exchange_request == req
    assert p_sender.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    assert p_sender.proposed_bed == env["bed2"]
    assert p_sender.is_active_reservation is True

    # Recipient participant
    p_recip = participants[1]
    assert p_recip.student == env["s2"]
    assert p_recip.exchange_request is None  # No fabricated request!
    assert p_recip.response_status == ProposalParticipant.ResponseStatus.PENDING
    assert p_recip.proposed_bed == env["bed1"]


# =========================================================================
# 2. Recipient Accepts and Declines
# =========================================================================

@pytest.mark.django_db
def test_recipient_accepts_proposal(institution, warden_user):
    """When recipient accepts, proposal advances to PENDING_APPROVAL and locks reservation."""
    ensure_roles_exist()
    _configure_policies(institution)
    env = _setup_two_bed_env(institution, warden_user, prefix="acc")

    req = create_exchange_request(
        student=env["s1"],
        data={"preferred_bed": env["bed2"], "reason": "Swap"},
    )
    proposal = ExchangeProposal.objects.get(created_by=env["s1"])

    # Recipient accepts
    part, updated_prop = respond_to_proposal(
        proposal_id=proposal.id,
        student=env["s2"],
        action="accept",
    )

    assert part.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    assert part.is_active_reservation is True
    assert updated_prop.status == ExchangeProposal.Status.PENDING_APPROVAL


@pytest.mark.django_db
def test_recipient_declines_proposal_reverts_sender_request(institution, warden_user):
    """When recipient declines, proposal is cancelled and sender's request reverts to OPEN."""
    ensure_roles_exist()
    _configure_policies(institution)
    env = _setup_two_bed_env(institution, warden_user, prefix="dec")

    req = create_exchange_request(
        student=env["s1"],
        data={"preferred_bed": env["bed2"], "reason": "Swap"},
    )
    proposal = ExchangeProposal.objects.get(created_by=env["s1"])

    # Recipient declines
    part, updated_prop = respond_to_proposal(
        proposal_id=proposal.id,
        student=env["s2"],
        action="decline",
    )

    assert part.response_status == ProposalParticipant.ResponseStatus.DECLINED
    assert updated_prop.status == ExchangeProposal.Status.CANCELLED

    # Reservations released
    assert not ProposalParticipant.objects.filter(proposal=proposal, is_active_reservation=True).exists()

    # Sender's request reverted to OPEN
    req.refresh_from_db()
    assert req.status == ExchangeRequest.Status.OPEN


# =========================================================================
# 3. Unauthorized Responses Blocked
# =========================================================================

@pytest.mark.django_db
def test_unauthorized_user_cannot_respond_to_invitation(institution, warden_user):
    """Only the designated recipient can respond; third parties and sender cannot respond as recipient."""
    ensure_roles_exist()
    _configure_policies(institution)
    env = _setup_two_bed_env(institution, warden_user, prefix="unauth")

    req = create_exchange_request(
        student=env["s1"],
        data={"preferred_bed": env["bed2"], "reason": "Swap"},
    )
    proposal = ExchangeProposal.objects.get(created_by=env["s1"])

    s3 = create_user_with_role(
        email="s3.unauth@apex.test",
        full_name="Third Party Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-S3-UNAUTH",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )

    with pytest.raises(PermissionDenied, match="not an authorized participant"):
        respond_to_proposal(
            proposal_id=proposal.id,
            student=s3,
            action="accept",
        )

    # Sender cannot decline after consenting via submission
    with pytest.raises(ValidationError, match="already accepted"):
        respond_to_proposal(
            proposal_id=proposal.id,
            student=env["s1"],
            action="decline",
        )


# =========================================================================
# 4. Same-Hostel Swap Executes After Single Warden Approval
# =========================================================================

@pytest.mark.django_db
def test_same_hostel_swap_executes_after_single_warden_approval(institution, warden_user):
    """Same-hostel swap is approved by assigned warden and executes atomically."""
    ensure_roles_exist()
    _configure_policies(institution)
    env = _setup_two_bed_env(institution, warden_user, prefix="sameapp")

    req = create_exchange_request(
        student=env["s1"],
        data={"preferred_bed": env["bed2"], "reason": "Same hostel swap"},
    )
    proposal = ExchangeProposal.objects.get(created_by=env["s1"])

    # Recipient accepts
    respond_to_proposal(proposal_id=proposal.id, student=env["s2"], action="accept")
    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert not proposal.is_cross_hostel

    # Warden approves and auto-executes
    appr, executed_prop = decide_exchange_proposal(
        proposal_id=proposal.id,
        actor=warden_user,
        decision="approved",
        remarks="Same hostel swap approved by assigned warden.",
        auto_execute=True,
    )

    assert executed_prop.status == ExchangeProposal.Status.COMPLETED
    assert Approval.objects.filter(proposal=proposal, decision=Approval.Decision.APPROVED).count() == 1

    # Occupancies updated
    env["occ1"].refresh_from_db()
    env["occ2"].refresh_from_db()
    assert env["occ1"].status == StudentOccupancy.OccupancyStatus.TRANSFERRED
    assert env["occ2"].status == StudentOccupancy.OccupancyStatus.TRANSFERRED

    new_occ1 = StudentOccupancy.objects.get(student=env["s1"], status=StudentOccupancy.OccupancyStatus.ACTIVE)
    new_occ2 = StudentOccupancy.objects.get(student=env["s2"], status=StudentOccupancy.OccupancyStatus.ACTIVE)
    assert new_occ1.bed_id == env["bed2"].id
    assert new_occ2.bed_id == env["bed1"].id

    # Transfer records created
    records = TransferRecord.objects.filter(proposal=proposal)
    assert records.count() == 2


# =========================================================================
# 5. Cross-Hostel Direct Exchange: Single Authority (Chief Warden Only)
# =========================================================================

@pytest.mark.django_db
def test_cross_hostel_direct_exchange_john_in_bh02_and_rohan_in_bh01(institution, warden_user):
    """
    Verification scenario:
    - John in BH-02 and Rohan in BH-01 direct exchange.
    - Rohan accepts -> proposal in Chief Warden queue; both hostel wardens denied if they try to decide.
    - Chief Warden approval swaps both beds once.
    - Repeated approval does not execute twice.
    """
    ensure_roles_exist()
    _configure_policies(institution)
    now = timezone.now()

    # Setup Hostels BH-01 and BH-02
    bh01 = Hostel.objects.create(
        institution=institution,
        name="Boys Hostel 01",
        code="BH-01",
        resident_category=User.ResidentialEligibility.MALE,
    )
    bh02 = Hostel.objects.create(
        institution=institution,
        name="Boys Hostel 02",
        code="BH-02",
        resident_category=User.ResidentialEligibility.MALE,
    )

    warden_bh01 = warden_user
    WardenAssignment.objects.create(warden=warden_bh01, hostel=bh01, is_primary=True)

    warden_bh02 = create_user_with_role(
        email="warden.bh02@apex.test",
        full_name="Warden BH02",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    WardenAssignment.objects.create(warden=warden_bh02, hostel=bh02, is_primary=True)

    chief_warden = create_user_with_role(
        email="chief.warden.direct@apex.test",
        full_name="Dr. Chief Warden",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )

    # Rooms & Beds
    b1 = Block.objects.create(hostel=bh01, name="Block A", code="BA")
    f1 = Floor.objects.create(block=b1, floor_number=1, floor_label="1F")
    r1 = Room.objects.create(floor=f1, room_number="101", operational_status=OperationalStatus.IN_SERVICE)
    bed_rohan = Bed.objects.create(room=r1, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)

    b2 = Block.objects.create(hostel=bh02, name="Block B", code="BB")
    f2 = Floor.objects.create(block=b2, floor_number=2, floor_label="2F")
    r2 = Room.objects.create(floor=f2, room_number="202", operational_status=OperationalStatus.IN_SERVICE)
    bed_john = Bed.objects.create(room=r2, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    # Students: Rohan in BH-01, John in BH-02
    rohan = create_user_with_role(
        email="rohan.sharma@apex.test",
        full_name="Rohan Sharma",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-ROHAN",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    john = create_user_with_role(
        email="john.doe@apex.test",
        full_name="John Doe",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-JOHN",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )

    occ_rohan = assign_student_to_bed(student=rohan, bed=bed_rohan, created_by=warden_bh01)
    occ_john = assign_student_to_bed(student=john, bed=bed_john, created_by=warden_bh02)

    # John selects BH-01, Room 101, Bed A occupied by Rohan
    req_john = create_exchange_request(
        student=john,
        data={
            "preferred_hostel": bh01,
            "preferred_bed": bed_rohan,
            "reason": "Direct cross-hostel exchange to Rohan's bed",
        },
    )

    proposal = ExchangeProposal.objects.get(created_by=john)
    assert proposal.is_cross_hostel is True

    # Rohan accepts without creating his own request
    respond_to_proposal(proposal_id=proposal.id, student=rohan, action="accept")
    proposal.refresh_from_db()
    assert proposal.status == ExchangeProposal.Status.PENDING_APPROVAL

    # 1. Authority Scoping Check
    cw_qs = get_approvals_queryset(chief_warden)
    assert cw_qs.filter(id=proposal.id).exists()
    assert get_pending_approvals_count(chief_warden) == 1

    # Neither hostel warden sees it in their queue or count
    assert not get_approvals_queryset(warden_bh01).filter(id=proposal.id).exists()
    assert not get_approvals_queryset(warden_bh02).filter(id=proposal.id).exists()
    assert get_pending_approvals_count(warden_bh01) == 0
    assert get_pending_approvals_count(warden_bh02) == 0

    can_cw, _ = can_user_decide_proposal(chief_warden, proposal)
    assert can_cw is True

    can_w1, reason1 = can_user_decide_proposal(warden_bh01, proposal)
    assert can_w1 is False
    assert "Cross-hostel proposals must be decided by the Chief Warden." in reason1

    can_w2, reason2 = can_user_decide_proposal(warden_bh02, proposal)
    assert can_w2 is False
    assert "Cross-hostel proposals must be decided by the Chief Warden." in reason2

    # 2. Server-side Enforcement: Both Wardens Denied (Approve and Reject)
    with pytest.raises(PermissionDenied, match="Cross-hostel proposals can only be decided by the Chief Warden"):
        decide_exchange_proposal(
            proposal_id=proposal.id,
            actor=warden_bh01,
            decision="approved",
            remarks="BH01 Warden trying to approve.",
        )

    with pytest.raises(PermissionDenied, match="Cross-hostel proposals can only be decided by the Chief Warden"):
        decide_exchange_proposal(
            proposal_id=proposal.id,
            actor=warden_bh02,
            decision="rejected",
            remarks="BH02 Warden trying to reject.",
        )

    # Allocations remain untouched
    occ_rohan.refresh_from_db()
    occ_john.refresh_from_db()
    assert occ_rohan.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ_john.status == StudentOccupancy.OccupancyStatus.ACTIVE

    # 3. Chief Warden Approves -> Atomically Swaps Both Beds Once
    appr, executed_prop = decide_exchange_proposal(
        proposal_id=proposal.id,
        actor=chief_warden,
        decision="approved",
        remarks="Cross-hostel exchange approved by Chief Warden.",
        auto_execute=True,
    )

    assert executed_prop.status == ExchangeProposal.Status.COMPLETED
    assert appr.authority_role == Approval.AuthorityRole.CHIEF_WARDEN

    occ_rohan.refresh_from_db()
    occ_john.refresh_from_db()
    assert occ_rohan.status == StudentOccupancy.OccupancyStatus.TRANSFERRED
    assert occ_john.status == StudentOccupancy.OccupancyStatus.TRANSFERRED

    new_occ_john = StudentOccupancy.objects.get(student=john, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    new_occ_rohan = StudentOccupancy.objects.get(student=rohan, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    assert new_occ_john.bed_id == bed_rohan.id
    assert new_occ_rohan.bed_id == bed_john.id

    records = TransferRecord.objects.filter(proposal=proposal)
    assert records.count() == 2

    # 4. Repeated Approval Must Not Execute Twice (Idempotent)
    appr_repeat, prop_repeat = decide_exchange_proposal(
        proposal_id=proposal.id,
        actor=chief_warden,
        decision="approved",
        remarks="Repeated approval submission.",
        auto_execute=True,
    )
    assert prop_repeat.status == ExchangeProposal.Status.COMPLETED
    assert TransferRecord.objects.filter(proposal=proposal).count() == 2
    assert StudentOccupancy.objects.filter(student=john, status=StudentOccupancy.OccupancyStatus.ACTIVE).count() == 1
    assert StudentOccupancy.objects.filter(student=rohan, status=StudentOccupancy.OccupancyStatus.ACTIVE).count() == 1


@pytest.mark.django_db
def test_existing_cross_hostel_proposal_with_warden_approval_requires_chief_warden_decision(institution, warden_user):
    """
    Existing cross-hostel proposal with 1 recorded warden approval:
    - Preserved in audit/approval history.
    - Requires Chief Warden decision before execution.
    - Warden decision and execution directly blocked.
    - Chief Warden decision completes execution.
    """
    ensure_roles_exist()
    _configure_policies(institution)
    now = timezone.now()

    h_a = Hostel.objects.create(institution=institution, name="North A", code="H-NA", resident_category=User.ResidentialEligibility.MALE)
    h_b = Hostel.objects.create(institution=institution, name="South B", code="H-SB", resident_category=User.ResidentialEligibility.MALE)
    WardenAssignment.objects.create(warden=warden_user, hostel=h_a, is_primary=True)

    warden_b = create_user_with_role(
        email="warden.b.legacy@apex.test",
        full_name="Warden B Legacy",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    WardenAssignment.objects.create(warden=warden_b, hostel=h_b, is_primary=True)

    chief_warden = create_user_with_role(
        email="chief.legacy@apex.test",
        full_name="Chief Warden Legacy",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )

    b_a = Block.objects.create(hostel=h_a, name="Block A", code="BA")
    f_a = Floor.objects.create(block=b_a, floor_number=1, floor_label="1F")
    r_a = Room.objects.create(floor=f_a, room_number="101", operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=r_a, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)

    b_b = Block.objects.create(hostel=h_b, name="Block B", code="BB")
    f_b = Floor.objects.create(block=b_b, floor_number=1, floor_label="1F")
    r_b = Room.objects.create(floor=f_b, room_number="201", operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=r_b, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    s1 = create_user_with_role(email="s1.leg@apex.test", full_name="Student 1 Leg", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-L1", residential_eligibility=User.ResidentialEligibility.MALE)
    s2 = create_user_with_role(email="s2.leg@apex.test", full_name="Student 2 Leg", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-L2", residential_eligibility=User.ResidentialEligibility.MALE)

    occ_a = assign_student_to_bed(student=s1, bed=bed_a, created_by=warden_user)
    occ_b = assign_student_to_bed(student=s2, bed=bed_b, created_by=warden_b)

    req1 = create_exchange_request(
        student=s1,
        data={"preferred_hostel": h_b, "preferred_bed": bed_b, "reason": "Cross hostel swap"},
    )
    prop = ExchangeProposal.objects.get(created_by=s1)
    respond_to_proposal(proposal_id=prop.id, student=s2, action="accept")
    prop.refresh_from_db()

    # Simulate existing legacy warden approval recorded previously
    Approval.objects.create(
        proposal=prop,
        authority_user=warden_user,
        authority_role=Approval.AuthorityRole.WARDEN,
        scope_hostel=h_a,
        decision=Approval.Decision.APPROVED,
        remarks="Legacy warden 1 approval.",
        decided_at=now,
    )

    # 1. If marked approved previously, execute_exchange_transfer still requires Chief Warden approval
    prop.status = ExchangeProposal.Status.APPROVED
    prop.save(update_fields=["status"])

    with pytest.raises(ValidationError, match="Chief Warden approval is required"):
        execute_exchange_transfer(proposal_id=prop.id, actor=chief_warden)

    # Reset to pending approval for authority decision test
    prop.status = ExchangeProposal.Status.PENDING_APPROVAL
    prop.save(update_fields=["status"])

    # 2. Hostel warden attempting to decide is denied
    can_decide_w, reason_w = can_user_decide_proposal(warden_b, prop)
    assert can_decide_w is False
    assert "Cross-hostel proposals must be decided by the Chief Warden." in reason_w

    with pytest.raises(PermissionDenied, match="Cross-hostel proposals can only be decided by the Chief Warden"):
        decide_exchange_proposal(proposal_id=prop.id, actor=warden_b, decision="approved", auto_execute=True)

    # 3. Chief Warden decides and executes
    appr, executed_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=chief_warden,
        decision="approved",
        remarks="Chief Warden decisive approval.",
        auto_execute=True,
    )
    assert executed_prop.status == ExchangeProposal.Status.COMPLETED
    assert TransferRecord.objects.filter(proposal=prop).count() == 2


@pytest.mark.django_db
def test_completed_detail_page_shows_no_pending_approvals(client, institution, warden_user):
    """
    Completed detail page shows 'Approved and executed', approver, remarks, and transfer orders.
    Never shows 'Awaiting Decision'.
    """
    ensure_roles_exist()
    _configure_policies(institution)
    chief_warden = create_user_with_role(
        email="cw.view.test@apex.test",
        full_name="Chief Warden View",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    env = _setup_two_bed_env(institution, warden_user, prefix="viewtest")

    req = create_exchange_request(
        student=env["s1"],
        data={"preferred_hostel": env["hostel"], "preferred_bed": env["bed2"], "reason": "Swap beds"},
    )
    prop = ExchangeProposal.objects.get(created_by=env["s1"])
    respond_to_proposal(proposal_id=prop.id, student=env["s2"], action="accept")

    # Assigned warden approves and executes
    decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        remarks="Approved and done.",
        auto_execute=True,
    )
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.COMPLETED

    client.force_login(warden_user)
    resp = client.get(reverse("exchange:approval_detail", kwargs={"pk": prop.id}))
    assert resp.status_code == 200
    content = resp.content.decode("utf-8")

    assert "Approved and executed" in content
    assert "Approved and done." in content
    assert "Awaiting Authority Decision" not in content
    assert "Awaiting Decision" not in content
    assert "Approve Exchange Proposal & Execute" not in content


# =========================================================================
# 6. Backwards Compatibility: Open Request Targeting Occupied Bed Becomes Actionable
# =========================================================================

@pytest.mark.django_db
def test_open_request_targeting_occupied_bed_becomes_actionable(institution, warden_user):
    """
    Requirement 5: Existing open requests targeting an occupied bed become actionable
    via activate_direct_occupied_bed_request if the occupant is verified.
    """
    ensure_roles_exist()
    _configure_policies(institution)
    now = timezone.now()
    env = _setup_two_bed_env(institution, warden_user, prefix="legacy")

    # Manually create a legacy OPEN request targeting occupied bed
    legacy_req = ExchangeRequest.objects.create(
        institution=institution,
        student=env["s1"],
        current_occupancy=env["occ1"],
        preferred_bed=env["bed2"],
        preferred_hostel=env["hostel"],
        request_type=ExchangeRequest.RequestType.EXCHANGE,
        status=ExchangeRequest.Status.OPEN,
        submitted_at=now,
        expires_at=now + timedelta(days=30),
    )

    # Activate it
    success, msg, activated_prop = activate_direct_occupied_bed_request(
        exchange_request=legacy_req,
        now=now,
    )

    assert success is True
    assert activated_prop is not None
    assert activated_prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    legacy_req.refresh_from_db()
    assert legacy_req.status == ExchangeRequest.Status.MATCHED

    # Recipient can now accept
    part, prop = respond_to_proposal(
        proposal_id=activated_prop.id,
        student=env["s2"],
        action="accept",
        now=now,
    )
    assert part.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL
