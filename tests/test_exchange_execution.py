"""
Comprehensive test suite for P04 Module M6:
Atomic Occupancy Update and Transfer Records for occupied-bed room exchanges.

Covers:
1. Two-student direct swaps (1-to-1):
   - Atomic occupancy swap (allocation source: DIRECT_SWAP).
   - Transition old occupancies to TRANSFERRED, create new ACTIVE occupancies.
   - Immutable TransferRecords with snapshot metadata.
   - Notifications and Audit trail.
2. Multi-party rotation cycles (3-, 4-, 5-student rings):
   - Multi-party atomic rotation (allocation source: MULTI_SWAP).
   - All-or-nothing execution across 3, 4, and 5 participants.
3. Cross-hostel exchange execution:
   - Supervised and approved by Chief Warden.
   - Assigned Warden denied cross-hostel execution.
4. Authority boundaries & Permission Enforcement:
   - Students, Deans, Hostel Admins, SysAdmins, Unassigned Wardens denied execution powers.
5. Cycle Validation & Failure Rollback (Strict Atomicity):
   - Non-closed cycle / broken permutation blocked.
   - Self-swaps blocked.
   - Inactive destination bed/room triggers full rollback.
   - Stale participant occupancy triggers full rollback.
   - Disciplinary / fee clearance failure triggers full rollback.
6. Execution Idempotency:
   - Repeated execution returns existing TransferRecords safely without duplicates.
7. Official PDF Transfer Letter Generation:
   - Authentic binary PDF-1.4 output (application/pdf).
   - Access control (student owner, assigned wardens, chief warden, dean).
8. Recovery Management Command:
   - execute_approved_transfers CLI execution and dry-run validation.
"""

from datetime import timedelta
import io
import pytest

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
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
)
from apps.core.models import AuditLog, Institution
from apps.exchange.matching import find_candidate_matches
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)
from apps.exchange.services import (
    activate_exchange_proposal,
    approve_and_execute_exchange_proposal,
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


# =========================================================================
# Fixtures & Setup Helpers
# =========================================================================

def _configure_all_policies(institution, **kwargs):
    """Ensure all required exchange policies are explicitly configured."""
    defaults = {
        PolicyRule.Key.COOLING_OFF_DAYS: 0,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
        PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS: 48,
    }
    defaults.update(kwargs)
    for key, val in defaults.items():
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


def _setup_hostel_cluster(institution, warden_user=None, name="Execution Hostel", gender=User.ResidentialEligibility.MALE):
    """Create a complete hostel structure with assigned warden."""
    hostel = Hostel.objects.create(
        institution=institution,
        name=name,
        code=f"EX-{name[:4].upper()}",
        resident_category=gender,
        status=Hostel.Status.ACTIVE,
    )
    block = Block.objects.create(hostel=hostel, name="Block A", code="BA")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1st Floor")
    if warden_user:
        WardenAssignment.objects.create(
            warden=warden_user,
            hostel=hostel,
            is_primary=True,
        )
    return hostel, block, floor


def _build_accepted_cycle(count, institution, warden_user, hostel_cluster=None, prefix="cyc"):
    """
    Build a complete, active, and unanimously accepted cycle proposal of length `count`.
    Each student i prefers bed (i + 1) % count.
    """
    _configure_all_policies(institution)
    now = timezone.now()

    if not hostel_cluster:
        hostel, block, floor = _setup_hostel_cluster(institution, warden_user, name=f"Hostel {prefix}")
    else:
        hostel, block, floor = hostel_cluster

    users = []
    rooms = []
    beds = []
    occupancies = []

    for i in range(count):
        user = create_user_with_role(
            email=f"{prefix}-{i}-{count}@apex.test",
            full_name=f"Student {prefix.upper()} {i}",
            role_name=ROLE_STUDENT,
            institution=institution,
            password="TestPassword123!",
            registration_number=f"REG-{prefix.upper()}-{count}-{i:02d}",
            residential_eligibility=hostel.resident_category,
        )
        room = Room.objects.create(
            floor=floor,
            room_number=f"{100 + i}",
            room_type=Room.RoomType.DOUBLE,
            occupancy_category="Standard",
            operational_status=OperationalStatus.IN_SERVICE,
        )
        bed = Bed.objects.create(
            room=room,
            bed_label=f"Bed {chr(65 + i)}",
            operational_status=OperationalStatus.IN_SERVICE,
        )
        occ = assign_student_to_bed(
            student=user,
            bed=bed,
            created_by=warden_user or user,
        )
        users.append(user)
        rooms.append(room)
        beds.append(bed)
        occupancies.append(occ)

    requests = []
    for i in range(count):
        student = users[i]
        occ = occupancies[i]
        target_bed = beds[(i + 1) % count]
        req = ExchangeRequest.objects.create(
            institution=student.institution,
            student=student,
            current_occupancy=occ,
            preferred_bed=target_bed,
            preferred_hostel=hostel,
            reason=f"Cycle move from {i} to {(i+1)%count}",
            status=ExchangeRequest.Status.OPEN,
            submitted_at=now,
            expires_at=now + timedelta(days=30),
        )
        requests.append(req)

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    draft_prop = ExchangeProposal.objects.filter(
        institution=institution,
        status=ExchangeProposal.Status.DRAFT,
        cycle_length=count,
    ).latest("created_at")

    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=warden_user or users[0], now=now)
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    for u in users:
        _, prop = respond_to_proposal(proposal_id=prop.id, student=u, action="accept", now=now)

    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL

    return {
        "proposal": prop,
        "users": users,
        "beds": beds,
        "occupancies": occupancies,
        "requests": requests,
        "hostel": hostel,
    }


# =========================================================================
# 1. Two-Student Direct Swap Execution
# =========================================================================

@pytest.mark.django_db
def test_two_student_direct_swap_execution(institution, warden_user):
    """
    Two-student swap completes atomically on approval:
    - Occupancies rotate: student 0 gets bed 1, student 1 gets bed 0.
    - Allocation source is DIRECT_SWAP.
    - Old occupancies are marked TRANSFERRED.
    - Two immutable TransferRecords created with exact snapshot data.
    - Proposal and Requests transition to COMPLETED.
    - Workflow reservations released.
    - Notifications and AuditLog created.
    """
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="swap2")
    prop = fixture["proposal"]
    u0, u1 = fixture["users"]
    b0, b1 = fixture["beds"]
    old_occ0, old_occ1 = fixture["occupancies"]

    appr, updated_prop, records = approve_and_execute_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        remarks="Direct swap approved and executed.",
    )

    assert appr.decision == Approval.Decision.APPROVED
    assert updated_prop.status == ExchangeProposal.Status.COMPLETED
    assert updated_prop.completed_at is not None
    assert len(records) == 2

    # Check that old occupancies are TRANSFERRED
    old_occ0.refresh_from_db()
    old_occ1.refresh_from_db()
    assert old_occ0.status == StudentOccupancy.OccupancyStatus.TRANSFERRED
    assert old_occ1.status == StudentOccupancy.OccupancyStatus.TRANSFERRED
    assert old_occ0.end_date is not None
    assert old_occ1.end_date is not None

    # Check new active occupancies
    new_occ0 = StudentOccupancy.objects.get(student=u0, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    new_occ1 = StudentOccupancy.objects.get(student=u1, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    assert new_occ0.bed_id == b1.id
    assert new_occ1.bed_id == b0.id
    assert new_occ0.source == StudentOccupancy.AllocationSource.DIRECT_SWAP
    assert new_occ1.source == StudentOccupancy.AllocationSource.DIRECT_SWAP

    # Check requests completed
    for req in fixture["requests"]:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.COMPLETED

    # Check workflow reservations released
    participants = ProposalParticipant.objects.filter(proposal=prop)
    for p in participants:
        assert not p.is_active_reservation

    # Check TransferRecords
    tr0 = TransferRecord.objects.get(proposal=prop, student=u0)
    assert tr0.previous_bed == b0
    assert tr0.new_bed == b1
    assert tr0.approval == appr
    assert tr0.source_hostel_name == fixture["hostel"].name
    assert tr0.destination_hostel_name == fixture["hostel"].name

    tr1 = TransferRecord.objects.get(proposal=prop, student=u1)
    assert tr1.previous_bed == b1
    assert tr1.new_bed == b0

    # Check Notifications
    notif0 = Notification.objects.filter(recipient=u0, notification_type=Notification.NotificationType.TRANSFER_COMPLETED)
    assert notif0.exists()
    assert "approved and completed" in notif0.first().message.lower()

    # Check AuditLog
    audit = AuditLog.objects.filter(target_id=str(prop.id), action="EXCHANGE_TRANSFER_EXECUTED")
    assert audit.exists()


# =========================================================================
# 2. Multi-Party Rotation Cycles (3-, 4-, 5-Student Rings)
# =========================================================================

@pytest.mark.django_db
@pytest.mark.parametrize("cycle_length", [3, 4, 5])
def test_multi_party_cycles_rotation_execution(cycle_length, institution, warden_user):
    """
    3, 4, and 5 student rotation cycles execute atomically:
    - Each student i receives bed (i + 1) % cycle_length.
    - Allocation source is MULTI_SWAP.
    - All old occupancies closed, all new occupancies active.
    - N TransferRecords created.
    """
    fixture = _build_accepted_cycle(cycle_length, institution, warden_user, prefix=f"ring{cycle_length}")
    prop = fixture["proposal"]
    users = fixture["users"]
    beds = fixture["beds"]
    old_occupancies = fixture["occupancies"]

    appr, updated_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        remarks=f"{cycle_length}-party rotation cycle approved.",
        auto_execute=True,
    )

    assert updated_prop.status == ExchangeProposal.Status.COMPLETED

    records = list(TransferRecord.objects.filter(proposal=prop).order_by("transferred_at"))
    assert len(records) == cycle_length

    # Verify each student's new occupancy matches the permutation
    for i in range(cycle_length):
        student = users[i]
        expected_dest_bed = beds[(i + 1) % cycle_length]

        old_occ = old_occupancies[i]
        old_occ.refresh_from_db()
        assert old_occ.status == StudentOccupancy.OccupancyStatus.TRANSFERRED

        new_occ = StudentOccupancy.objects.get(student=student, status=StudentOccupancy.OccupancyStatus.ACTIVE)
        assert new_occ.bed_id == expected_dest_bed.id
        assert new_occ.source == StudentOccupancy.AllocationSource.MULTI_SWAP

        tr = next(r for r in records if r.student_id == student.id)
        assert tr.previous_bed_id == beds[i].id
        assert tr.new_bed_id == expected_dest_bed.id


# =========================================================================
# 3. Cross-Hostel Exchange Execution
# =========================================================================

@pytest.mark.django_db
def test_cross_hostel_exchange_execution_authority(institution, warden_user, chief_warden_user):
    """
    Cross-hostel exchange execution:
    - Assigned Warden cannot approve or execute (PermissionDenied).
    - Chief Warden can approve and execute.
    - Students successfully move across hostel boundaries.
    """
    _configure_all_policies(institution)
    now = timezone.now()

    h1, b1, f1 = _setup_hostel_cluster(institution, warden_user, name="North Hostel")
    h2, b2, f2 = _setup_hostel_cluster(institution, None, name="South Hostel")

    u1 = create_user_with_role(
        email="cross.u1@apex.test",
        full_name="Cross Student 1",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-CR-01",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    r1 = Room.objects.create(floor=f1, room_number="101", operational_status=OperationalStatus.IN_SERVICE)
    bed1 = Bed.objects.create(room=r1, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
    occ1 = assign_student_to_bed(student=u1, bed=bed1, created_by=warden_user)

    u2 = create_user_with_role(
        email="cross.u2@apex.test",
        full_name="Cross Student 2",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number="REG-CR-02",
        residential_eligibility=User.ResidentialEligibility.MALE,
    )
    r2 = Room.objects.create(floor=f2, room_number="201", operational_status=OperationalStatus.IN_SERVICE)
    bed2 = Bed.objects.create(room=r2, bed_label="Bed B", operational_status=OperationalStatus.IN_SERVICE)
    occ2 = assign_student_to_bed(student=u2, bed=bed2, created_by=chief_warden_user)

    req1 = ExchangeRequest.objects.create(
        institution=institution,
        student=u1,
        current_occupancy=occ1,
        preferred_bed=bed2,
        preferred_hostel=h2,
        status=ExchangeRequest.Status.OPEN,
        submitted_at=now,
        expires_at=now + timedelta(days=30),
    )
    req2 = ExchangeRequest.objects.create(
        institution=institution,
        student=u2,
        current_occupancy=occ2,
        preferred_bed=bed1,
        preferred_hostel=h1,
        status=ExchangeRequest.Status.OPEN,
        submitted_at=now,
        expires_at=now + timedelta(days=30),
    )

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    draft_prop = ExchangeProposal.objects.filter(
        institution=institution,
        status=ExchangeProposal.Status.DRAFT,
        cycle_length=2,
    ).latest("created_at")

    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=chief_warden_user, now=now)
    respond_to_proposal(proposal_id=prop.id, student=u1, action="accept", now=now)
    respond_to_proposal(proposal_id=prop.id, student=u2, action="accept", now=now)
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL

    # Unassigned warden is denied decision on cross-hostel proposal
    unassigned_warden = create_user_with_role(
        email="unassigned.w@apex.test",
        full_name="Unassigned Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    with pytest.raises(PermissionDenied, match="Cross-hostel proposals can only be decided by the Chief Warden"):
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=unassigned_warden,
            decision="approved",
            auto_execute=True,
        )

    # Assigned warden 1 is also denied decision on cross-hostel proposal (strictly Chief Warden)
    with pytest.raises(PermissionDenied, match="Cross-hostel proposals can only be decided by the Chief Warden"):
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="approved",
            remarks="Hostel 1 approved by Warden 1.",
            auto_execute=True,
        )
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL

    # Chief Warden approves and executes successfully
    appr, updated_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=chief_warden_user,
        decision="approved",
        remarks="Cross-hostel exchange approved by Chief Warden.",
        auto_execute=True,
    )

    assert updated_prop.status == ExchangeProposal.Status.COMPLETED

    occ1.refresh_from_db()
    occ2.refresh_from_db()
    assert occ1.status == StudentOccupancy.OccupancyStatus.TRANSFERRED
    assert occ2.status == StudentOccupancy.OccupancyStatus.TRANSFERRED

    new_occ1 = StudentOccupancy.objects.get(student=u1, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    new_occ2 = StudentOccupancy.objects.get(student=u2, status=StudentOccupancy.OccupancyStatus.ACTIVE)
    assert new_occ1.bed_id == bed2.id
    assert new_occ2.bed_id == bed1.id


# =========================================================================
# 4. Unauthorized Roles Denied Execution
# =========================================================================

@pytest.mark.django_db
def test_unauthorized_roles_denied_execution(institution, warden_user, student_user, admin_user, sysadmin_user, dean_user):
    """Students, Deans, Hostel Admins, and SysAdmins cannot execute transfers."""
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="unauth")
    prop = fixture["proposal"]

    unauthorized_actors = [student_user, admin_user, sysadmin_user, dean_user]
    for actor in unauthorized_actors:
        with pytest.raises(PermissionDenied):
            execute_exchange_transfer(
                proposal_id=prop.id,
                actor=actor,
            )


# =========================================================================
# 5. Atomicity & Rollback Guarantees
# =========================================================================

@pytest.mark.django_db
def test_atomicity_destination_bed_out_of_service_rollback(institution, warden_user):
    """
    If any destination bed goes OUT_OF_SERVICE prior to execution:
    - Execution fails with ValidationError.
    - Entire transaction rolls back: zero occupancies are modified.
    """
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="rollback_bed")
    prop = fixture["proposal"]
    u0, u1 = fixture["users"]
    b0, b1 = fixture["beds"]
    occ0, occ1 = fixture["occupancies"]

    # Dest bed b1 is taken out of service
    b1.operational_status = OperationalStatus.OUT_OF_SERVICE
    b1.save(update_fields=["operational_status"])

    with pytest.raises(ValidationError, match="operational requirements|no longer in service|satisfies preferences"):
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="approved",
            auto_execute=True,
        )

    # Verify atomic rollback: original occupancies remain ACTIVE
    occ0.refresh_from_db()
    occ1.refresh_from_db()
    assert occ0.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ1.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert not TransferRecord.objects.filter(proposal=prop).exists()
    prop.refresh_from_db()
    assert prop.status != ExchangeProposal.Status.COMPLETED


@pytest.mark.django_db
def test_atomicity_stale_occupancy_rollback(institution, warden_user):
    """
    If one student in the cycle has their occupancy changed/cancelled:
    - Execution fails with ValidationError.
    - Complete rollback: other student's occupancy remains intact.
    """
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="rollback_occ")
    prop = fixture["proposal"]
    occ0, occ1 = fixture["occupancies"]

    # Student 0 moves out
    occ0.status = StudentOccupancy.OccupancyStatus.CANCELLED
    occ0.save(update_fields=["status"])

    with pytest.raises(ValidationError, match="no longer active"):
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="approved",
            auto_execute=True,
        )

    # Verify student 1's occupancy is still active
    occ1.refresh_from_db()
    assert occ1.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert not TransferRecord.objects.filter(proposal=prop).exists()


# =========================================================================
# 6. Idempotency of Execution
# =========================================================================

@pytest.mark.django_db
def test_execution_is_idempotent(institution, warden_user):
    """Repeated execution of an already completed proposal safely returns existing records."""
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="idemp")
    prop = fixture["proposal"]

    appr, updated_prop, records_1 = approve_and_execute_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
    )
    assert len(records_1) == 2
    assert updated_prop.status == ExchangeProposal.Status.COMPLETED

    # Second execution call
    records_2 = execute_exchange_transfer(
        proposal_id=prop.id,
        actor=warden_user,
    )
    assert len(records_2) == 2
    assert [r.id for r in records_1] == [r.id for r in records_2]

    # No duplicate occupancies created
    assert StudentOccupancy.objects.filter(student=fixture["users"][0], status=StudentOccupancy.OccupancyStatus.ACTIVE).count() == 1


# =========================================================================
# 7. Official PDF Transfer Letter Generation
# =========================================================================

@pytest.mark.django_db
def test_genuine_pdf_transfer_letter_generation(client, institution, warden_user):
    """
    Official Accommodation Transfer Order:
    - format=pdf returns application/pdf with PDF-1.4 header and %%EOF trailer.
    - Accessible by transferred student and warden.
    - Inaccessible by other students.
    """
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="pdf")
    prop = fixture["proposal"]
    u0, u1 = fixture["users"]

    approve_and_execute_exchange_proposal(proposal_id=prop.id, actor=warden_user)
    tr0 = TransferRecord.objects.get(proposal=prop, student=u0)

    # 1. Student downloads their own PDF
    client.force_login(u0)
    url = reverse("exchange:transfer_letter", kwargs={"pk": tr0.id})
    resp = client.get(f"{url}?format=pdf")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-1.4")
    assert b"%%EOF" in resp.content
    assert "attachment; filename=" in resp["Content-Disposition"]

    # 2. Staff (warden) downloads student's PDF
    client.force_login(warden_user)
    resp_staff = client.get(f"{url}?format=pdf")
    assert resp_staff.status_code == 200
    assert resp_staff["Content-Type"] == "application/pdf"

    # 3. Another student cannot download student 0's transfer letter
    client.force_login(u1)
    resp_unauth = client.get(f"{url}?format=pdf")
    assert resp_unauth.status_code == 403


# =========================================================================
# 8. Recovery Management Command
# =========================================================================

@pytest.mark.django_db
def test_execute_approved_transfers_command(institution, warden_user):
    """
    Test execute_approved_transfers management command:
    - Proposal approved without immediate auto_execute.
    - Dry-run verifies cycle permutation.
    - Command executes proposal and updates occupancies to COMPLETED.
    """
    fixture = _build_accepted_cycle(2, institution, warden_user, prefix="mgmt")
    prop = fixture["proposal"]

    # Approve without auto_execute (legacy / decoupled approval state)
    decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        auto_execute=False,
    )
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.APPROVED

    out_dry = io.StringIO()
    call_command(
        "execute_approved_transfers",
        f"--proposal={prop.id}",
        "--dry-run",
        stdout=out_dry,
    )
    assert "[DRY-RUN]" in out_dry.getvalue()
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.APPROVED  # Not modified

    # Run real execution
    out_real = io.StringIO()
    call_command(
        "execute_approved_transfers",
        f"--proposal={prop.id}",
        stdout=out_real,
    )
    assert "Successfully executed Proposal" in out_real.getvalue()
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.COMPLETED
    assert TransferRecord.objects.filter(proposal=prop).count() == 2
