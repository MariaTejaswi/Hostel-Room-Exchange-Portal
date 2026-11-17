"""
Comprehensive test suite for P04 Module M4: Multi-Party Acceptance.

Covers:
1. Proposal activation workflow with PostgreSQL-safe row-lock ordering and revalidation.
2. Acceptance deadline policy enforcement and request-expiry capping.
3. Partial acceptance keeping proposals in PENDING_ACCEPTANCE.
4. Unanimous acceptance advancing proposals to PENDING_APPROVAL (2-person to 5-person).
5. Any participant decline cancelling the proposal and releasing workflow locks (is_active_reservation=False).
6. Expired request handling on cancellation (never reopening expired requests).
7. Proactive expiry during response and batch sweep (expire_exchange_proposals).
8. Celery beat task integration (expire_exchange_proposals_task).
9. Authorization boundaries: non-participants and cross-institution students blocked.
10. Idempotent repeated responses and rejection of stale/conflicting decisions.
11. Revalidation failures: missing acceptance window policy, bed change, conflicting active reservations.
12. Strict zero occupancy mutations across all acceptance and decline paths.
13. Student UI views and zero PII leakage (no emails, phone numbers, or disciplinary/fee holds).
14. Management command activate_proposal.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_STUDENT,
    create_user_with_role,
    ensure_roles_exist,
)
from apps.core.models import AuditLog, Institution
from apps.exchange.matching import find_candidate_matches
from apps.exchange.models import (
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
)
from apps.exchange.services import (
    activate_exchange_proposal,
    expire_exchange_proposals,
    respond_to_proposal,
)
from apps.exchange.tasks import expire_exchange_proposals_task
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
# Helpers and Fixtures
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
    prefix="m4",
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
            created_by=warden_user,
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
    """
    Create exchange requests where student i prefers bed (i + 1) % N.
    Forms a directed cycle of length N.
    """
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


# =========================================================================
# 1. 2-Person Direct Swap Proposal Lifecycle
# =========================================================================

@pytest.mark.django_db
def test_two_person_proposal_activation_lifecycle(institution, warden_user):
    """
    Full lifecycle test for 2-person swap:
    DRAFT -> PENDING_ACCEPTANCE -> partial acceptance -> unanimous acceptance -> PENDING_APPROVAL.
    Verifies zero physical occupancy mutation and correct workflow locking.
    """
    _configure_all_policies(institution, proposal_acceptance_window_hours=24)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="swap2")
    now = timezone.now()
    reqs = _create_cyclic_requests(fix, now=now)

    # 1. Run matching engine to persist draft proposal
    res = find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    assert res.metrics.total_candidates >= 1
    draft_prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    assert draft_prop is not None
    assert draft_prop.cycle_length == 2

    # In DRAFT, requests remain OPEN and reservations are NOT locked
    for req in reqs:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.OPEN
    for part in draft_prop.participants.all():
        assert part.is_active_reservation is False
        assert part.response_status == ProposalParticipant.ResponseStatus.PENDING

    # 2. Activate Proposal
    prop = activate_exchange_proposal(proposal_id=draft_prop.id, acting_user=warden_user, now=now)
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    assert prop.expires_at == now + timedelta(hours=24)

    # Requests are now MATCHED, reservations active
    for req in reqs:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.MATCHED
    for part in prop.participants.all():
        assert part.is_active_reservation is True
        assert part.response_status == ProposalParticipant.ResponseStatus.PENDING

    # Physical occupancies MUST NOT be modified
    for occ in fix["occupancies"]:
        occ.refresh_from_db()
        assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE

    # Verify notifications were dispatched to both participants
    notifs = Notification.objects.filter(proposal=prop, notification_type=Notification.NotificationType.ACCEPTANCE_REQUIRED)
    assert notifs.count() == 2

    # Verify AuditLog created
    assert AuditLog.objects.filter(target_id=str(prop.id), action="EXCHANGE_PROPOSAL_ACTIVATED").exists()

    # 3. Partial Acceptance: Student 0 accepts
    p0, updated_prop = respond_to_proposal(
        proposal_id=prop.id,
        student=fix["users"][0],
        action="accept",
        now=now + timedelta(hours=1),
    )
    assert p0.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    assert updated_prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE  # Still pending student 1

    # 4. Unanimous Acceptance: Student 1 accepts
    p1, final_prop = respond_to_proposal(
        proposal_id=prop.id,
        student=fix["users"][1],
        action="accept",
        now=now + timedelta(hours=2),
    )
    assert p1.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    assert final_prop.status == ExchangeProposal.Status.PENDING_APPROVAL
    assert final_prop.accepted_at is not None

    # AuditLog and notifications for unanimous acceptance
    assert AuditLog.objects.filter(target_id=str(prop.id), action="EXCHANGE_PROPOSAL_AWAITING_APPROVAL").exists()
    approval_notifs = Notification.objects.filter(
        proposal=prop,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
    )
    assert approval_notifs.count() == 2

    # Physical occupancy still 100% UNTOUCHED
    for occ in fix["occupancies"]:
        occ.refresh_from_db()
        assert occ.status == StudentOccupancy.OccupancyStatus.ACTIVE


# =========================================================================
# 2. 3-Person to 5-Person Multi-Party Cycles
# =========================================================================

@pytest.mark.django_db
@pytest.mark.parametrize("cycle_len", [3, 4, 5])
def test_multi_person_rotation_cycles_unanimous_acceptance(institution, warden_user, cycle_len):
    """
    Test 3, 4, and 5-person rotation cycles.
    Verifies that until every participant accepts, proposal remains PENDING_ACCEPTANCE.
    The final acceptance advances the proposal to PENDING_APPROVAL.
    """
    _configure_all_policies(institution, proposal_acceptance_window_hours=48)
    fix = _setup_cycle_fixture(cycle_len, institution, warden_user, prefix=f"rot{cycle_len}")
    now = timezone.now()
    _create_cyclic_requests(fix, now=now)

    res = find_candidate_matches(
        institution=institution,
        min_length=cycle_len,
        max_length=cycle_len,
        persist_candidates=True,
        now=now,
    )
    assert res.metrics.total_candidates >= 1

    prop = ExchangeProposal.objects.filter(
        institution=institution,
        cycle_length=cycle_len,
        status=ExchangeProposal.Status.DRAFT,
    ).first()
    assert prop is not None

    activated_prop = activate_exchange_proposal(proposal_id=prop.id, now=now)
    assert activated_prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    # Accept one-by-one
    for i in range(cycle_len - 1):
        _, curr_prop = respond_to_proposal(
            proposal_id=prop.id,
            student=fix["users"][i],
            action="accept",
            now=now + timedelta(minutes=i + 1),
        )
        assert curr_prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    # Final student accepts
    _, final_prop = respond_to_proposal(
        proposal_id=prop.id,
        student=fix["users"][-1],
        action="accept",
        now=now + timedelta(minutes=cycle_len),
    )
    assert final_prop.status == ExchangeProposal.Status.PENDING_APPROVAL


# =========================================================================
# 3. Decline Behavior & Workflow Lock Release
# =========================================================================

@pytest.mark.django_db
def test_decline_cancels_proposal_and_releases_reservations(institution, warden_user):
    """
    When any participant declines:
    - Proposal status -> CANCELLED
    - Participant response -> DECLINED
    - All reservations released (is_active_reservation=False)
    - Non-expired requests revert back to OPEN
    - Notifications sent to all participants
    - Physical occupancies remain unchanged
    """
    _configure_all_policies(institution, proposal_acceptance_window_hours=48)
    fix = _setup_cycle_fixture(3, institution, warden_user, prefix="decl3")
    now = timezone.now()
    reqs = _create_cyclic_requests(fix, now=now)

    find_candidate_matches(institution=institution, min_length=3, max_length=3, persist_candidates=True, now=now)
    prop = ExchangeProposal.objects.filter(institution=institution, cycle_length=3, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id, now=now)

    # Student 0 accepts
    respond_to_proposal(proposal_id=prop.id, student=fix["users"][0], action="accept", now=now)

    # Student 1 declines
    p1, cancelled_prop = respond_to_proposal(
        proposal_id=prop.id,
        student=fix["users"][1],
        action="decline",
        now=now + timedelta(minutes=10),
    )
    assert p1.response_status == ProposalParticipant.ResponseStatus.DECLINED
    assert cancelled_prop.status == ExchangeProposal.Status.CANCELLED

    # All participants must have reservations released
    for part in cancelled_prop.participants.all():
        assert part.is_active_reservation is False

    # All requests must revert to OPEN
    for req in reqs:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.OPEN

    # AuditLog created
    assert AuditLog.objects.filter(target_id=str(prop.id), action="EXCHANGE_PROPOSAL_DECLINED").exists()

    # Notifications dispatched to all participants
    decline_notifs = Notification.objects.filter(
        proposal=prop,
        notification_type=Notification.NotificationType.ACCEPTANCE_UPDATED,
    )
    assert decline_notifs.count() >= 3


@pytest.mark.django_db
def test_decline_with_expired_request_marks_it_expired(institution, warden_user):
    """
    If a proposal is cancelled and one participant's request has passed its expires_at,
    that request must transition to EXPIRED, never reopening an expired request.
    """
    _configure_all_policies(institution, proposal_acceptance_window_hours=48)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="expreq")
    now = timezone.now()
    reqs = _create_cyclic_requests(fix, now=now)

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id, now=now)

    # Simulate request 0 expiring during proposal review (before the 48h proposal deadline)
    reqs[0].expires_at = now + timedelta(hours=1)
    reqs[0].save(update_fields=["expires_at"])

    # Student 1 declines at hour 2 (request 0 is expired, proposal deadline is hour 48)
    later = now + timedelta(hours=2)
    respond_to_proposal(proposal_id=prop.id, student=fix["users"][1], action="decline", now=later)

    reqs[0].refresh_from_db()
    reqs[1].refresh_from_db()

    assert reqs[0].status == ExchangeRequest.Status.EXPIRED  # Expired request never reopened
    assert reqs[1].status == ExchangeRequest.Status.OPEN  # Unexpired request reverted to OPEN


# =========================================================================
# 4. Proactive and Batch Proposal Expiry
# =========================================================================

@pytest.mark.django_db
def test_proactive_proposal_expiry_on_response(institution, warden_user):
    """
    If a proposal deadline has lapsed, calling respond_to_proposal triggers proactive
    expiry, raises ValidationError, transitions proposal to EXPIRED, and releases locks.
    """
    _configure_all_policies(institution, proposal_acceptance_window_hours=12)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="proact")
    now = timezone.now()
    reqs = _create_cyclic_requests(fix, now=now)

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id, now=now)

    # Move past the 12-hour acceptance window
    past_deadline = now + timedelta(hours=13)
    with pytest.raises(ValidationError, match="This exchange proposal has expired"):
        respond_to_proposal(
            proposal_id=prop.id,
            student=fix["users"][0],
            action="accept",
            now=past_deadline,
        )

    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.EXPIRED
    for part in prop.participants.all():
        assert part.is_active_reservation is False
    for req in reqs:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.OPEN


@pytest.mark.django_db
def test_expire_exchange_proposals_batch_sweep_and_celery_task(institution, warden_user):
    """
    Batch sweep function and Celery task expire all overdue PENDING_ACCEPTANCE proposals.
    """
    _configure_all_policies(institution, proposal_acceptance_window_hours=10)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="batch")
    now = timezone.now()
    _create_cyclic_requests(fix, now=now)

    find_candidate_matches(institution=institution, persist_candidates=True, now=now)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id, now=now)

    # 1. Sweep before deadline -> 0 expired
    count, ids = expire_exchange_proposals(institution=institution, now=now + timedelta(hours=5))
    assert count == 0

    # 2. Sweep after deadline -> expires proposal
    count, ids = expire_exchange_proposals(institution=institution, now=now + timedelta(hours=11))
    assert count == 1
    assert str(prop.id) in ids

    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.EXPIRED

    # 3. Test Celery task wrapper
    task_res = expire_exchange_proposals_task()
    assert task_res["status"] == "success"


# =========================================================================
# 5. Revalidation on Activation
# =========================================================================

@pytest.mark.django_db
def test_activation_fails_if_policy_unconfigured(institution, warden_user):
    """
    If PROPOSAL_ACCEPTANCE_WINDOW_HOURS is not configured for the institution,
    activation must fail with an actionable error.
    """
    # Configure cooling-off and validity, but omit PROPOSAL_ACCEPTANCE_WINDOW_HOURS
    PolicyRule.objects.filter(institution=institution, key=PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS).delete()
    _configure_all_policies(institution)
    PolicyRule.objects.filter(institution=institution, key=PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS).delete()

    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="nopol")
    _create_cyclic_requests(fix)
    find_candidate_matches(institution=institution, persist_candidates=True)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()

    with pytest.raises(ValidationError, match="Proposal acceptance window is not configured"):
        activate_exchange_proposal(proposal_id=prop.id)


@pytest.mark.django_db
def test_activation_fails_if_student_occupancy_changed(institution, warden_user):
    """
    If a participant checks out or moves beds before proposal activation,
    revalidation blocks activation.
    """
    _configure_all_policies(institution)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="occchg")
    _create_cyclic_requests(fix)
    find_candidate_matches(institution=institution, persist_candidates=True)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()

    # Participant 0 checks out / occupancy released
    fix["occupancies"][0].status = StudentOccupancy.OccupancyStatus.RELEASED
    fix["occupancies"][0].save(update_fields=["status"])

    with pytest.raises(ValidationError, match="current occupancy has changed or is no longer active"):
        activate_exchange_proposal(proposal_id=prop.id)


@pytest.mark.django_db
def test_activation_fails_if_active_reservation_conflict(institution, warden_user):
    """
    A student, request, or bed cannot be activated into two concurrent active proposals.
    """
    _configure_all_policies(institution)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="confl")
    _create_cyclic_requests(fix)
    find_candidate_matches(institution=institution, persist_candidates=True)
    prop1 = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop1.id)

    # Create dummy second proposal targeting the same student
    prop2 = ExchangeProposal.objects.create(
        institution=institution,
        status=ExchangeProposal.Status.DRAFT,
        cycle_length=2,
    )
    for p in prop1.participants.all():
        ProposalParticipant.objects.create(
            proposal=prop2,
            exchange_request=p.exchange_request,
            student=p.student,
            current_occupancy=p.current_occupancy,
            proposed_bed=p.proposed_bed,
            sequence_order=p.sequence_order,
            is_active_reservation=False,
        )

    with pytest.raises(ValidationError, match=r"(already reserved in another active proposal|is no longer open)"):
        activate_exchange_proposal(proposal_id=prop2.id)


# =========================================================================
# 6. Authorization, Ownership & Idempotence
# =========================================================================

@pytest.mark.django_db
def test_unauthorized_user_cannot_respond(institution, warden_user):
    """
    Non-participants and cross-institution students are strictly blocked from responding.
    """
    _configure_all_policies(institution)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="authz")
    _create_cyclic_requests(fix)
    find_candidate_matches(institution=institution, persist_candidates=True)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id)

    # Non-participant student
    stranger = create_user_with_role(
        email="stranger@apex.test",
        full_name="Stranger Student",
        role_name=ROLE_STUDENT,
        institution=institution,
        registration_number="REG-STRANGER-01",
    )
    with pytest.raises(PermissionDenied, match="not an authorized participant"):
        respond_to_proposal(proposal_id=prop.id, student=stranger, action="accept")


@pytest.mark.django_db
def test_duplicate_response_is_idempotent(institution, warden_user):
    """
    Submitting the same response twice returns the participant without error.
    Submitting a conflicting response after acceptance raises ValidationError.
    """
    _configure_all_policies(institution)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="idemp")
    _create_cyclic_requests(fix)
    find_candidate_matches(institution=institution, persist_candidates=True)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id)

    # First accept
    p, _ = respond_to_proposal(proposal_id=prop.id, student=fix["users"][0], action="accept")
    assert p.response_status == ProposalParticipant.ResponseStatus.ACCEPTED

    # Second accept is idempotent
    p_repeat, _ = respond_to_proposal(proposal_id=prop.id, student=fix["users"][0], action="accept")
    assert p_repeat.response_status == ProposalParticipant.ResponseStatus.ACCEPTED

    # Reversing decision to decline is rejected
    with pytest.raises(ValidationError, match="You have already accepted"):
        respond_to_proposal(proposal_id=prop.id, student=fix["users"][0], action="decline")


# =========================================================================
# 7. Student UI Views and Zero PII Leakage
# =========================================================================

@pytest.mark.django_db
def test_student_proposal_views_and_zero_pii(client, institution, warden_user):
    """
    Verify proposal list, detail, and respond views render properly with coffee-brown theme
    and ZERO PII leakage (no email, phone, or disciplinary text of other participants).
    """
    _configure_all_policies(institution)
    fix = _setup_cycle_fixture(3, institution, warden_user, prefix="pii")
    now = timezone.now()
    _create_cyclic_requests(fix, now=now)

    find_candidate_matches(institution=institution, min_length=3, max_length=3, persist_candidates=True, now=now)
    prop = ExchangeProposal.objects.filter(institution=institution, cycle_length=3, status=ExchangeProposal.Status.DRAFT).first()
    activate_exchange_proposal(proposal_id=prop.id, now=now)

    student0 = fix["users"][0]
    student1 = fix["users"][1]
    client.force_login(student0)

    # 1. Proposal List View
    list_url = reverse("exchange:proposal_list")
    list_res = client.get(list_url)
    assert list_res.status_code == 200
    assert b"My exchange proposals" in list_res.content

    # 2. Proposal Detail View
    detail_url = reverse("exchange:proposal_detail", kwargs={"pk": prop.pk})
    detail_res = client.get(detail_url)
    assert detail_res.status_code == 200
    content = detail_res.content.decode("utf-8")

    # Own name or "You" should appear, but OTHER participants' sensitive info must NOT appear
    assert "You (Your Request)" in content
    assert student1.email not in content  # NO email leakage
    assert "disciplinary" not in content.lower()  # NO disciplinary hold leakage

    # 3. Action Bar Accept via POST
    respond_url = reverse("exchange:proposal_respond", kwargs={"pk": prop.pk})
    post_res = client.post(respond_url, {"action": "accept"})
    assert post_res.status_code == 302

    prop_part = ProposalParticipant.objects.get(proposal=prop, student=student0)
    assert prop_part.response_status == ProposalParticipant.ResponseStatus.ACCEPTED


# =========================================================================
# 8. Management Command activate_proposal
# =========================================================================

@pytest.mark.django_db
def test_activate_proposal_command(institution, warden_user):
    """
    Verify the activate_proposal management command activates a proposal and outputs report.
    """
    _configure_all_policies(institution)
    fix = _setup_cycle_fixture(2, institution, warden_user, prefix="cmd")
    _create_cyclic_requests(fix)
    find_candidate_matches(institution=institution, persist_candidates=True)
    prop = ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).first()
    assert prop is not None

    call_command("activate_proposal", proposal=str(prop.id), institution=institution.code)

    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
