"""
Comprehensive test suite for P04 Module M5: Approval Workflow with Remarks.

Covers:
1. Authority Boundaries:
   - Same-hostel exchanges: assigned Warden of that hostel only.
   - Cross-hostel exchanges: Chief Warden of the same institution.
   - Unassigned Warden denied approval of same-hostel proposals.
   - Warden cannot approve cross-hostel proposals.
   - Chief Warden cannot approve same-hostel proposals without warden assignment.
   - Deans, Hostel Admins, System Admins, and Students denied decision authority.
   - Cross-institution decisions denied.
2. Preconditions:
   - Proposal must be in PENDING_APPROVAL.
   - Every participant accepted on time.
   - Cycle length and allocations match.
   - Ineligible participant or stale allocation blocks approval.
   - Acceptance deadline passing after unanimous acceptance does NOT expire or block proposal.
3. Decision Mechanics:
   - Approval retains workflow reservations (is_active_reservation=True) for M6 transfer execution.
   - Approval sets status to APPROVED ("Approved — transfer pending").
   - Occupancy is strictly unchanged (zero physical room/bed updates).
   - Rejection requires non-empty remarks.
   - Rejection releases workflow reservations (is_active_reservation=False).
   - Rejection reverts unexpired requests to OPEN; expired requests remain EXPIRED.
   - Audit logging and notifications dispatched.
   - Repeated decisions by same actor are idempotent.
4. HTTP Views & Scoping:
   - Warden queue filtered to same-hostel proposals in assigned hostels.
   - Chief Warden queue filtered to cross-hostel proposals.
   - Dean queue has read-only access to all proposals.
   - Unauthorized roles receive 403 Forbidden.
   - Web POST approval and rejection workflow with flash messages.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
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
from apps.core.models import AuditLog, Institution
from apps.exchange.matching import find_candidate_matches
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
)
from apps.exchange.selectors import (
    can_user_decide_proposal,
    get_approval_proposal_detail,
    get_approvals_queryset,
    get_pending_approvals_count,
)
from apps.exchange.services import (
    activate_exchange_proposal,
    decide_exchange_proposal,
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
# Fixtures and Helpers
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


def _setup_cycle_fixture(
    count,
    institution,
    warden_user,
    hostel=None,
    prefix="m5",
    gender=User.ResidentialEligibility.MALE,
):
    """Set up count students with active occupancies in distinct rooms."""
    ensure_roles_exist()
    if hostel is None:
        hostel = Hostel.objects.create(
            institution=institution,
            name=f"Hostel {prefix.upper()}",
            code=f"H-{prefix[:3].upper()}",
            resident_category=gender,
        )
    if warden_user:
        WardenAssignment.objects.get_or_create(
            warden=warden_user,
            hostel=hostel,
            defaults={"is_primary": True},
        )

    block = Block.objects.create(hostel=hostel, name=f"Block {prefix}", code=f"B-{prefix[:2].upper()}")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1F")

    users = []
    rooms = []
    beds = []
    occupancies = []

    for i in range(count):
        user = create_user_with_role(
            email=f"{prefix}-{i}@apex.test",
            full_name=f"Student {prefix.capitalize()} {i}",
            role_name=ROLE_STUDENT,
            institution=institution,
            password="TestPassword123!",
            registration_number=f"REG-{prefix.upper()}-{i:03d}",
            residential_eligibility=gender,
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
            bed_label="Bed A",
            operational_status=OperationalStatus.IN_SERVICE,
        )
        occupancy = assign_student_to_bed(
            student=user,
            bed=bed,
            created_by=warden_user or user,
        )
        users.append(user)
        rooms.append(room)
        beds.append(bed)
        occupancies.append(occupancy)

    return {
        "hostel": hostel,
        "users": users,
        "rooms": rooms,
        "beds": beds,
        "occupancies": occupancies,
    }


def _create_cyclic_requests(fixture, now=None):
    """Create cyclic exchange requests: student i prefers bed (i + 1) % N."""
    now = now or timezone.now()
    count = len(fixture["users"])
    requests = []
    for i in range(count):
        student = fixture["users"][i]
        occ = fixture["occupancies"][i]
        target_bed = fixture["beds"][(i + 1) % count]
        req = ExchangeRequest.objects.create(
            institution=student.institution,
            student=student,
            current_occupancy=occ,
            preferred_bed=target_bed,
            preferred_hostel=target_bed.room.floor.block.hostel,
            reason=f"Cyclic swap preference from {i} to {(i+1)%count}",
            status=ExchangeRequest.Status.OPEN,
            submitted_at=now,
            expires_at=now + timedelta(days=30),
        )
        requests.append(req)
    return requests


def _build_accepted_proposal(count, institution, warden_user, hostel=None, prefix="acc"):
    """Helper to create, activate, and unanimously accept a proposal advancing it to PENDING_APPROVAL."""
    _configure_all_policies(institution)
    now = timezone.now()
    fix = _setup_cycle_fixture(count, institution, warden_user, hostel=hostel, prefix=prefix)
    _create_cyclic_requests(fix, now=now)
    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    draft_prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).latest("created_at")
    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=warden_user or fix["users"][0], now=now)
    # Everyone accepts
    for u in fix["users"]:
        prop = respond_to_proposal(proposal_id=prop.id, student=u, action="accept", now=now)[1]
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL
    return prop, fix


# =========================================================================
# 1. Assigned Warden Same-Hostel Approval
# =========================================================================

@pytest.mark.django_db
def test_assigned_warden_approves_same_hostel_proposal(institution, warden_user):
    """
    Assigned Warden approves a valid same-hostel proposal:
    - Status transitions to APPROVED.
    - Approval record created with authority_role=WARDEN, scope_hostel=hostel.
    - Workflow reservations are retained (is_active_reservation=True).
    - Physical occupancy is strictly untouched (no room moves yet).
    - Requests remain MATCHED.
    - Audit log and notifications created.
    """
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="same_warden")
    assert not prop.is_cross_hostel
    assert prop.scope_hostel == fix["hostel"]

    original_bed_ids = [occ.bed_id for occ in fix["occupancies"]]

    # Warden approves
    appr, updated_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        remarks="Approved after room review.",
    )

    assert updated_prop.status == ExchangeProposal.Status.APPROVED
    assert appr.decision == Approval.Decision.APPROVED
    assert appr.authority_user == warden_user
    assert appr.authority_role == Approval.AuthorityRole.WARDEN
    assert appr.scope_hostel == fix["hostel"]
    assert appr.remarks == "Approved after room review."
    assert appr.decided_at is not None

    # Verify workflow reservations remain active
    for part in updated_prop.participants.all():
        assert part.is_active_reservation is True

    # Verify requests remain MATCHED
    for req in ExchangeRequest.objects.filter(student__in=fix["users"]):
        assert req.status == ExchangeRequest.Status.MATCHED

    # Verify occupancy is STRICTLY UNCHANGED (Zero physical moves in M5)
    for i, user in enumerate(fix["users"]):
        active_occ = StudentOccupancy.objects.get(student=user, status=StudentOccupancy.OccupancyStatus.ACTIVE)
        assert active_occ.bed_id == original_bed_ids[i]

    # Verify audit log
    assert AuditLog.objects.filter(
        institution=institution,
        action="EXCHANGE_PROPOSAL_APPROVED",
        target_id=str(prop.id),
    ).exists()

    # Verify notification dispatched
    assert Notification.objects.filter(
        recipient__in=fix["users"],
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
    ).count() == 2


# =========================================================================
# 2. Unassigned Warden Denied
# =========================================================================

@pytest.mark.django_db
def test_unassigned_warden_denied_same_hostel_approval(institution, warden_user):
    """A Warden not assigned to the proposal's hostel cannot approve the proposal."""
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="unassigned_w")

    # Create another warden not assigned to fix['hostel']
    other_warden = create_user_with_role(
        email="other.warden@apex.test",
        full_name="Other Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )

    with pytest.raises(PermissionDenied) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=other_warden,
            decision="approved",
            remarks="Unauthorized attempt.",
        )
    assert "not assigned to supervise hostel" in str(exc_info.value)

    # Proposal remains pending
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert not Approval.objects.filter(proposal=prop).exists()


# =========================================================================
# 3. Warden Cannot Approve Cross-Hostel Proposal
# =========================================================================

@pytest.mark.django_db
def test_warden_cannot_approve_cross_hostel_proposal(institution, warden_user):
    """Wardens cannot approve cross-hostel proposals (only Chief Warden can)."""
    ensure_roles_exist()
    _configure_all_policies(institution)
    now = timezone.now()

    # Create 2 hostels
    hostel_a = Hostel.objects.create(institution=institution, name="Hostel Cross A", code="H-CRA", resident_category=User.ResidentialEligibility.MALE)
    hostel_b = Hostel.objects.create(institution=institution, name="Hostel Cross B", code="H-CRB", resident_category=User.ResidentialEligibility.MALE)
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel_a, is_primary=True)

    block_a = Block.objects.create(hostel=hostel_a, name="Block A", code="BA")
    floor_a = Floor.objects.create(block=block_a, floor_number=1, floor_label="1F")
    room_a = Room.objects.create(floor=floor_a, room_number="101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room_a, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)

    block_b = Block.objects.create(hostel=hostel_b, name="Block B", code="BB")
    floor_b = Floor.objects.create(block=block_b, floor_number=1, floor_label="1F")
    room_b = Room.objects.create(floor=floor_b, room_number="201", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room_b, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    s1 = create_user_with_role(email="s1@apex.test", full_name="Student 1", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-CR1", residential_eligibility=User.ResidentialEligibility.MALE)
    s2 = create_user_with_role(email="s2@apex.test", full_name="Student 2", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-CR2", residential_eligibility=User.ResidentialEligibility.MALE)

    warden_b = create_user_with_role(
        email="warden.b.cross@apex.test",
        full_name="Warden B Cross",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="Password123!",
    )
    WardenAssignment.objects.create(warden=warden_b, hostel=hostel_b, is_primary=True)

    occ_a = assign_student_to_bed(student=s1, bed=bed_a, created_by=warden_user)
    occ_b = assign_student_to_bed(student=s2, bed=bed_b, created_by=warden_b)

    ExchangeRequest.objects.create(institution=institution, student=s1, current_occupancy=occ_a, preferred_bed=bed_b, preferred_hostel=hostel_b, status=ExchangeRequest.Status.OPEN, submitted_at=now, expires_at=now + timedelta(days=30))
    ExchangeRequest.objects.create(institution=institution, student=s2, current_occupancy=occ_b, preferred_bed=bed_a, preferred_hostel=hostel_a, status=ExchangeRequest.Status.OPEN, submitted_at=now, expires_at=now + timedelta(days=30))

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    draft_prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).latest("created_at")
    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=warden_user, now=now)
    respond_to_proposal(proposal_id=prop.id, student=s1, action="accept", now=now)
    prop = respond_to_proposal(proposal_id=prop.id, student=s2, action="accept", now=now)[1]

    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert prop.is_cross_hostel is True

    # Unassigned warden attempts to approve: strictly denied
    unassigned_w = create_user_with_role(
        email="unassigned.cw.test@apex.test",
        full_name="Unassigned Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="Password123!",
    )
    with pytest.raises(PermissionDenied) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=unassigned_w,
            decision="approved",
            remarks="Trying to approve cross hostel without Chief Warden role.",
        )
    assert "cross-hostel proposals can only be decided by the chief warden" in str(exc_info.value).lower()

    # Assigned warden A is also strictly denied (both approve and reject) under confirmed single Chief Warden policy
    with pytest.raises(PermissionDenied) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="approved",
            remarks="Hostel A approved by Warden A.",
        )
    assert "cross-hostel proposals can only be decided by the chief warden" in str(exc_info.value).lower()

    with pytest.raises(PermissionDenied) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_b,
            decision="rejected",
            remarks="Hostel B rejected by Warden B.",
        )
    assert "cross-hostel proposals can only be decided by the chief warden" in str(exc_info.value).lower()


# =========================================================================
# 4. Chief Warden Cross-Hostel Approval
# =========================================================================

@pytest.mark.django_db
def test_chief_warden_approves_cross_hostel_proposal(institution, warden_user):
    """Chief Warden approves cross-hostel proposal successfully."""
    ensure_roles_exist()
    _configure_all_policies(institution)
    now = timezone.now()

    chief_warden = create_user_with_role(
        email="chief.warden@apex.test",
        full_name="Dr. Chief Warden",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )

    # 2 Hostels
    hostel_a = Hostel.objects.create(institution=institution, name="Hostel North", code="H-NTH", resident_category=User.ResidentialEligibility.MALE)
    hostel_b = Hostel.objects.create(institution=institution, name="Hostel South", code="H-STH", resident_category=User.ResidentialEligibility.MALE)
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel_a, is_primary=True)

    block_a = Block.objects.create(hostel=hostel_a, name="Block N", code="BN")
    floor_a = Floor.objects.create(block=block_a, floor_number=1, floor_label="1F")
    room_a = Room.objects.create(floor=floor_a, room_number="101", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_a = Bed.objects.create(room=room_a, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)

    block_b = Block.objects.create(hostel=hostel_b, name="Block S", code="BS")
    floor_b = Floor.objects.create(block=block_b, floor_number=1, floor_label="1F")
    room_b = Room.objects.create(floor=floor_b, room_number="201", room_type=Room.RoomType.DOUBLE, operational_status=OperationalStatus.IN_SERVICE)
    bed_b = Bed.objects.create(room=room_b, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    s1 = create_user_with_role(email="s_north@apex.test", full_name="Student North", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-NTH-1", residential_eligibility=User.ResidentialEligibility.MALE)
    s2 = create_user_with_role(email="s_south@apex.test", full_name="Student South", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-STH-1", residential_eligibility=User.ResidentialEligibility.MALE)

    occ_a = assign_student_to_bed(student=s1, bed=bed_a, created_by=warden_user)
    occ_b = assign_student_to_bed(student=s2, bed=bed_b, created_by=chief_warden)

    ExchangeRequest.objects.create(institution=institution, student=s1, current_occupancy=occ_a, preferred_bed=bed_b, preferred_hostel=hostel_b, status=ExchangeRequest.Status.OPEN, submitted_at=now, expires_at=now + timedelta(days=30))
    ExchangeRequest.objects.create(institution=institution, student=s2, current_occupancy=occ_b, preferred_bed=bed_a, preferred_hostel=hostel_a, status=ExchangeRequest.Status.OPEN, submitted_at=now, expires_at=now + timedelta(days=30))

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    draft_prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).latest("created_at")
    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=warden_user, now=now)
    respond_to_proposal(proposal_id=prop.id, student=s1, action="accept", now=now)
    prop = respond_to_proposal(proposal_id=prop.id, student=s2, action="accept", now=now)[1]

    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert prop.is_cross_hostel is True

    # Chief Warden approves
    appr, updated_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=chief_warden,
        decision="approved",
        remarks="Cross-hostel approval granted by Chief Warden.",
    )

    assert updated_prop.status == ExchangeProposal.Status.APPROVED
    assert appr.decision == Approval.Decision.APPROVED
    assert appr.authority_user == chief_warden
    assert appr.authority_role == Approval.AuthorityRole.CHIEF_WARDEN
    assert appr.scope_hostel is None
    assert appr.remarks == "Cross-hostel approval granted by Chief Warden."


# =========================================================================
# 5. Chief Warden Cannot Approve Same-Hostel Proposal Without Assignment
# =========================================================================

@pytest.mark.django_db
def test_chief_warden_cannot_approve_same_hostel_proposal(institution, warden_user):
    """Chief Warden cannot approve same-hostel proposals (only assigned Wardens can)."""
    ensure_roles_exist()
    chief_warden = create_user_with_role(
        email="cw.apex@apex.test",
        full_name="Chief Warden Apex",
        role_name=ROLE_CHIEF_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="cw_same")
    assert not prop.is_cross_hostel

    with pytest.raises(PermissionDenied) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=chief_warden,
            decision="approved",
            remarks="Chief warden attempting same-hostel decision.",
        )
    assert "assigned hostel warden" in str(exc_info.value).lower()


# =========================================================================
# 6. Unauthorized Roles Denied Approval
# =========================================================================

@pytest.mark.django_db
def test_unauthorized_roles_denied_approval(institution, warden_user):
    """Students, Deans, Hostel Admins, and SysAdmins cannot decide proposals."""
    ensure_roles_exist()
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="unauth_roles")

    dean = create_user_with_role(email="dean@apex.test", full_name="Dean Welfare", role_name=ROLE_DEAN, institution=institution, password="Password123!")
    admin = create_user_with_role(email="hadmin@apex.test", full_name="Hostel Admin", role_name=ROLE_HOSTEL_ADMIN, institution=institution, password="Password123!")
    sysadmin = create_user_with_role(email="sadmin@apex.test", full_name="Sys Admin", role_name=ROLE_SYSADMIN, institution=institution, password="Password123!")
    student = fix["users"][0]

    for user in [dean, admin, sysadmin, student]:
        with pytest.raises(PermissionDenied):
            decide_exchange_proposal(
                proposal_id=prop.id,
                actor=user,
                decision="approved",
                remarks="Unauthorized role attempt.",
            )


# =========================================================================
# 7. Cross-Institution Decision Denied
# =========================================================================

@pytest.mark.django_db
def test_cross_institution_decision_denied(institution, warden_user):
    """A Warden from another institution cannot decide proposals."""
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="cross_inst")

    other_inst = Institution.objects.create(name="Other University", code="OTHER")
    other_warden = create_user_with_role(
        email="warden@other.test",
        full_name="Other Inst Warden",
        role_name=ROLE_WARDEN,
        institution=other_inst,
        password="TestPassword123!",
    )

    with pytest.raises(PermissionDenied) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=other_warden,
            decision="approved",
            remarks="Foreign institution attempt.",
        )
    assert "proposal institution" in str(exc_info.value).lower()


# =========================================================================
# 8. Rejection Requires Non-Empty Remarks
# =========================================================================

@pytest.mark.django_db
def test_rejection_requires_remarks(institution, warden_user):
    """Rejecting a proposal without remarks raises ValidationError."""
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="rej_remarks")

    with pytest.raises(ValidationError) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="rejected",
            remarks="",
        )
    assert "rejection reason/remarks is required" in str(exc_info.value).lower()


# =========================================================================
# 9. Rejection Releases Reservations and Reverts Unexpired Requests
# =========================================================================

@pytest.mark.django_db
def test_rejection_releases_reservations_and_reverts_requests(institution, warden_user):
    """
    Rejection releases reservations, reverts unexpired requests to OPEN,
    and leaves expired requests EXPIRED.
    """
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="rej_flow")

    # Set one request as unexpired and one as expired
    req_unexp = ExchangeRequest.objects.get(student=fix["users"][0])
    req_exp = ExchangeRequest.objects.get(student=fix["users"][1])

    now = timezone.now()
    req_unexp.expires_at = now + timedelta(days=10)
    req_unexp.save(update_fields=["expires_at"])

    # Ensure created_at < expires_at for the expired request to satisfy DB constraint
    ExchangeRequest.objects.filter(id=req_exp.id).update(
        created_at=now - timedelta(days=40),
        expires_at=now - timedelta(days=1),
    )

    appr, updated_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="rejected",
        remarks="Room inspection failed. Structural repair needed.",
    )

    assert updated_prop.status == ExchangeProposal.Status.REJECTED
    assert appr.decision == Approval.Decision.REJECTED
    assert appr.remarks == "Room inspection failed. Structural repair needed."

    # Verify workflow reservations released
    for part in updated_prop.participants.all():
        assert part.is_active_reservation is False

    # Verify unexpired reverted to OPEN, expired remains EXPIRED
    req_unexp.refresh_from_db()
    req_exp.refresh_from_db()
    assert req_unexp.status == ExchangeRequest.Status.OPEN
    assert req_exp.status == ExchangeRequest.Status.EXPIRED

    # Verify audit log and notification
    assert AuditLog.objects.filter(
        institution=institution,
        action="EXCHANGE_PROPOSAL_REJECTED",
        target_id=str(prop.id),
    ).exists()

    assert Notification.objects.filter(
        recipient__in=fix["users"],
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
    ).count() == 2


# =========================================================================
# 10. Preconditions Block Invalid Proposals
# =========================================================================

@pytest.mark.django_db
def test_precondition_blocks_non_pending_approval_proposals(institution, warden_user):
    """Proposals not in PENDING_APPROVAL cannot be decided."""
    _configure_all_policies(institution)
    now = timezone.now()
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="pre_pending")
    _create_cyclic_requests(fix, now=now)
    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    draft_prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).latest("created_at")

    # In DRAFT status
    with pytest.raises(ValidationError) as exc_info:
        decide_exchange_proposal(
            proposal_id=draft_prop.id,
            actor=warden_user,
            decision="approved",
            remarks="Approving draft.",
        )
    assert "not awaiting approval" in str(exc_info.value).lower()

    # In PENDING_ACCEPTANCE status (only 1 student accepted)
    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=warden_user, now=now)
    respond_to_proposal(proposal_id=prop.id, student=fix["users"][0], action="accept", now=now)
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    with pytest.raises(ValidationError):
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="approved",
            remarks="Premature approval.",
        )


@pytest.mark.django_db
def test_precondition_blocks_stale_occupancy(institution, warden_user):
    """If a student moves out or bed allocation changes, approval is blocked."""
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="stale_occ")

    # Change occupancy of student 0
    occ = fix["occupancies"][0]
    occ.status = StudentOccupancy.OccupancyStatus.RELEASED
    occ.save(update_fields=["status"])

    with pytest.raises(ValidationError) as exc_info:
        decide_exchange_proposal(
            proposal_id=prop.id,
            actor=warden_user,
            decision="approved",
            remarks="Approving with stale occupancy.",
        )
    assert "no longer active" in str(exc_info.value).lower()


# =========================================================================
# 11. Approval After Acceptance Deadline Remains Valid
# =========================================================================

@pytest.mark.django_db
def test_approval_after_acceptance_deadline_remains_valid(institution, warden_user):
    """
    If participants accepted before deadline, subsequent passage of the
    acceptance deadline does NOT block or expire authority approval.
    """
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="post_deadline")
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL

    # Time advances 5 days past proposal expires_at
    future_time = prop.expires_at + timedelta(days=5)

    # Authority approves at future_time
    appr, updated_prop = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        remarks="Approved after weekend review.",
        now=future_time,
    )

    assert updated_prop.status == ExchangeProposal.Status.APPROVED
    assert appr.decision == Approval.Decision.APPROVED


# =========================================================================
# 12. Idempotent Repeated Decisions
# =========================================================================

@pytest.mark.django_db
def test_idempotent_approval_decision(institution, warden_user):
    """Submitting the same decision twice by the same actor is safe and idempotent."""
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="idempotent")

    appr1, prop1 = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        remarks="First submission.",
    )

    # Second submission
    appr2, prop2 = decide_exchange_proposal(
        proposal_id=prop.id,
        actor=warden_user,
        decision="approved",
        remarks="Second submission.",
    )

    assert appr1.id == appr2.id
    assert prop1.status == prop2.status == ExchangeProposal.Status.APPROVED


# =========================================================================
# 13. Selectors & Scoping Helpers
# =========================================================================

@pytest.mark.django_db
def test_approval_selectors_scoping(institution, warden_user):
    """
    Test get_approvals_queryset and get_pending_approvals_count:
    - Warden sees only same-hostel proposals in assigned hostel.
    - Chief Warden sees only cross-hostel proposals.
    - Dean sees all non-draft proposals.
    """
    ensure_roles_exist()
    chief_warden = create_user_with_role(email="cw.scope@apex.test", full_name="Chief Warden Scope", role_name=ROLE_CHIEF_WARDEN, institution=institution, password="Password123!")
    dean = create_user_with_role(email="dean.scope@apex.test", full_name="Dean Scope", role_name=ROLE_DEAN, institution=institution, password="Password123!")

    # Same hostel proposal in warden_user's hostel
    prop_same, fix_same = _build_accepted_proposal(2, institution, warden_user, prefix="scope_same")

    # Warden queryset should include prop_same
    warden_qs = get_approvals_queryset(warden_user)
    assert prop_same in warden_qs
    assert get_pending_approvals_count(warden_user) == 1

    # Chief Warden queryset should NOT include same-hostel proposal
    cw_qs = get_approvals_queryset(chief_warden)
    assert prop_same not in cw_qs
    assert get_pending_approvals_count(chief_warden) == 0

    # Dean sees prop_same
    dean_qs = get_approvals_queryset(dean)
    assert prop_same in dean_qs

    # can_user_decide_proposal checks
    can_decide_w, _ = can_user_decide_proposal(warden_user, prop_same)
    assert can_decide_w is True

    can_decide_cw, reason_cw = can_user_decide_proposal(chief_warden, prop_same)
    assert can_decide_cw is False
    assert "Warden" in reason_cw

    can_decide_dean, reason_dean = can_user_decide_proposal(dean, prop_same)
    assert can_decide_dean is False
    assert "read-only" in reason_dean.lower()


# =========================================================================
# 14. HTTP Views & Scoping (List, Detail, Decide)
# =========================================================================

@pytest.mark.django_db
def test_approval_http_views_workflow(client, institution, warden_user):
    """Full HTTP test: list view, detail view, decide view (POST)."""
    ensure_roles_exist()
    prop, fix = _build_accepted_proposal(2, institution, warden_user, prefix="http_view")

    # 1. Anonymous redirected to login
    url_list = reverse("exchange:approval_list")
    resp = client.get(url_list)
    assert resp.status_code == 302
    assert "/login/" in resp.url

    # 2. Student gets 403 Forbidden
    client.force_login(fix["users"][0])
    resp = client.get(url_list)
    assert resp.status_code == 403

    # 3. Warden accesses list view
    client.force_login(warden_user)
    resp = client.get(url_list)
    assert resp.status_code == 200
    assert "Exchange Approvals Queue" in resp.content.decode()
    assert str(prop.id)[:8] in resp.content.decode()

    # 4. Warden accesses detail view
    url_detail = reverse("exchange:approval_detail", kwargs={"pk": prop.id})
    resp = client.get(url_detail)
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Proposal Review & Authority Evaluation" in content
    assert "Approve Exchange Proposal" in content
    assert "Reject Proposal" in content

    # 5. POST Reject without remarks fails with validation message
    url_decide = reverse("exchange:approval_decide", kwargs={"pk": prop.id})
    resp = client.post(url_decide, {"decision": "rejected", "remarks": ""}, follow=True)
    assert resp.status_code == 200
    assert "Remarks are mandatory when rejecting" in resp.content.decode()
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.PENDING_APPROVAL

    # 6. POST Approve succeeds
    resp = client.post(url_decide, {"decision": "approved", "remarks": "Approved by warden via web."}, follow=True)
    assert resp.status_code == 200
    assert "approved successfully" in resp.content.decode()
    prop.refresh_from_db()
    assert prop.status in (ExchangeProposal.Status.APPROVED, ExchangeProposal.Status.COMPLETED)
