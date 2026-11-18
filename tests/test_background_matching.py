"""
Comprehensive test suite for P04 Background Matching & Orchestration Workflow.

Verifies end-to-end background matching, reliable scheduling, deterministic selection,
cycle suppression, student transparency, failure resilience, and concurrency safety:
1. Two compatible requests trigger background matching and produce an active proposal.
2. Four-person rotation cycle detected and activated in background.
3. Requests with no compatible matches remain OPEN with persisted 'no match' status.
4. Rapid successive requests coalesce without running redundant matching passes (needs_rerun).
5. Concurrent workers do not activate conflicting proposals for the same student/bed.
6. Preference change during matching invalidates stale candidate.
7. Request withdrawal or expiry prevents activation.
8. Declined proposal suppresses that cycle from immediate reactivation.
9. Updating preferences unsuppresses previously declined cycle.
10. Broker failure leaves durable outbox record that reconciliation recovers.
11. Vacant-bed transfers never enter matching.
12. Institutional isolation is strictly preserved.
13. Matching and activation never mutate physical occupancy.
"""

from datetime import timedelta
import uuid
import pytest
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_STUDENT,
    ROLE_WARDEN,
    create_user_with_role,
)
from apps.core.models import AuditLog, Institution
from apps.exchange.matching import find_candidate_matches
from apps.exchange.models import (
    ExchangeProposal,
    ExchangeRequest,
    MatchingJob,
    MatchingOutbox,
    Notification,
    PolicyRule,
    ProposalParticipant,
    SuppressedCycle,
)
from apps.exchange.services import (
    activate_exchange_proposal,
    create_exchange_request as _orig_create_exchange_request,
    is_candidate_suppressed,
    reconcile_all_institutions_matching,
    record_cycle_suppression,
    respond_to_proposal,
    run_institution_matching,
    schedule_institution_matching,
    update_exchange_request,
    withdraw_exchange_request,
)


def create_exchange_request(*args, **kwargs):
    kwargs.setdefault("direct_invitation", False)
    return _orig_create_exchange_request(*args, **kwargs)
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

def _configure_policies(institution, acceptance_window_hours=48):
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.COOLING_OFF_DAYS,
        defaults={"value": {"value": 0}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.REQUEST_VALIDITY_DAYS,
        defaults={"value": {"value": 30}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
        defaults={"value": {"value": False}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD,
        defaults={"value": {"value": False}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS,
        defaults={"value": {"value": acceptance_window_hours}, "is_active": True},
    )


def _setup_hostel_env(institution, name="Test Hostel", prefix="th"):
    warden = create_user_with_role(
        email=f"warden-{prefix}@apex.test",
        full_name=f"Warden {prefix.upper()}",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    hostel = Hostel.objects.create(
        institution=institution,
        name=name,
        code=f"H-{prefix.upper()}",
        resident_category=User.ResidentialEligibility.MALE,
        status=Hostel.Status.ACTIVE,
    )
    WardenAssignment.objects.create(warden=warden, hostel=hostel, is_primary=True)
    block = Block.objects.create(hostel=hostel, name="Block A", code="BA")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1st Floor")
    return hostel, block, floor, warden


def _create_student_with_bed(institution, hostel, floor, index, prefix="std"):
    user = create_user_with_role(
        email=f"{prefix}-{index}@apex.test",
        full_name=f"Student {prefix.upper()} {index}",
        role_name=ROLE_STUDENT,
        institution=institution,
        password="TestPassword123!",
        registration_number=f"REG-{prefix.upper()}-{index:03d}",
        residential_eligibility=hostel.resident_category,
    )
    room = Room.objects.create(
        floor=floor,
        room_number=f"{100 + index}",
        room_type=Room.RoomType.DOUBLE,
        occupancy_category="Standard",
        operational_status=OperationalStatus.IN_SERVICE,
    )
    bed = Bed.objects.create(
        room=room,
        bed_label=f"Bed {chr(65 + (index % 26))}",
        operational_status=OperationalStatus.IN_SERVICE,
    )
    occ = assign_student_to_bed(student=user, bed=bed)
    return user, room, bed, occ


# =========================================================================
# Test Suite
# =========================================================================

@pytest.mark.django_db
def test_two_compatible_requests_trigger_and_activate_proposal():
    """1. Two compatible requests trigger background matching and produce an active proposal."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="swp")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="swp")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="swp")

    # Student 1 wants Bed 2
    req1 = create_exchange_request(
        student=u1,
        data={
            "preferred_bed": b2,
            "reason": "Want bed 2",
        },
    )
    assert req1.status == ExchangeRequest.Status.OPEN
    assert req1.matching_status == ExchangeRequest.MatchingStatus.WAITING

    # Student 2 wants Bed 1
    req2 = create_exchange_request(
        student=u2,
        data={
            "preferred_bed": b1,
            "reason": "Want bed 1",
        },
    )
    assert req2.status == ExchangeRequest.Status.OPEN
    assert req2.matching_status == ExchangeRequest.MatchingStatus.WAITING

    # Execute background matching
    job = run_institution_matching(institution_id=inst.id, trigger_reason="test_swap")
    assert job.status == MatchingJob.Status.COMPLETED
    assert job.proposals_activated_count == 1

    # Verify proposal activated
    prop = ExchangeProposal.objects.filter(institution=inst, cycle_length=2).latest("created_at")
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    assert prop.expires_at is not None

    req1.refresh_from_db()
    req2.refresh_from_db()
    assert req1.status == ExchangeRequest.Status.MATCHED
    assert req1.matching_status == ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE
    assert req2.status == ExchangeRequest.Status.MATCHED
    assert req2.matching_status == ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE

    # Check workflow reservations
    parts = list(prop.participants.all())
    assert len(parts) == 2
    assert all(p.is_active_reservation for p in parts)

    # Check notification dispatched
    notifs = Notification.objects.filter(proposal=prop, notification_type=Notification.NotificationType.ACCEPTANCE_REQUIRED)
    assert notifs.count() == 2


@pytest.mark.django_db
def test_four_person_rotation_cycle_detected_and_activated():
    """2. 4-person rotation cycle detected and activated in background."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="rot4")

    students = []
    beds = []
    for i in range(4):
        u, r, b, occ = _create_student_with_bed(inst, hostel, floor, i, prefix="rot4")
        students.append(u)
        beds.append(b)

    # 4-cycle: 0 -> 1 -> 2 -> 3 -> 0
    requests = []
    for i in range(4):
        target_b = beds[(i + 1) % 4]
        req = create_exchange_request(
            student=students[i],
            data={
                "preferred_bed": target_b,
                "reason": f"Move {i} to {(i+1)%4}",
            },
        )
        requests.append(req)

    job = run_institution_matching(institution_id=inst.id, trigger_reason="test_4cycle")
    assert job.status == MatchingJob.Status.COMPLETED
    assert job.proposals_activated_count == 1

    prop = ExchangeProposal.objects.filter(institution=inst, cycle_length=4).latest("created_at")
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    for req in requests:
        req.refresh_from_db()
        assert req.status == ExchangeRequest.Status.MATCHED
        assert req.matching_status == ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE


@pytest.mark.django_db
def test_requests_with_no_compatible_matches_remain_open():
    """3. Requests with no compatible matches remain OPEN with persisted 'no match' status."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="nomatch")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="nm")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="nm")

    # u1 wants b2, but u2 does not create a request
    req1 = create_exchange_request(
        student=u1,
        data={
            "preferred_bed": b2,
            "reason": "Want bed 2 only",
        },
    )

    job = run_institution_matching(institution_id=inst.id, trigger_reason="test_nomatch")
    assert job.status == MatchingJob.Status.COMPLETED
    assert job.proposals_activated_count == 0

    req1.refresh_from_db()
    assert req1.status == ExchangeRequest.Status.OPEN
    assert req1.matching_status == ExchangeRequest.MatchingStatus.NO_MATCH
    assert req1.last_matched_at is not None


@pytest.mark.django_db
def test_rapid_successive_requests_coalesce_with_needs_rerun():
    """4. Rapid successive requests coalesce without running redundant matching passes."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)

    # Simulate a currently RUNNING job
    running_job = MatchingJob.objects.create(
        institution=inst,
        status=MatchingJob.Status.RUNNING,
        trigger_reason="first_trigger",
        started_at=timezone.now(),
    )

    # Calling run_institution_matching while another is RUNNING flags needs_rerun
    res_job = run_institution_matching(institution_id=inst.id, trigger_reason="second_trigger")
    assert res_job.id == running_job.id
    running_job.refresh_from_db()
    assert running_job.needs_rerun is True


@pytest.mark.django_db
def test_concurrent_workers_do_not_activate_conflicting_proposals():
    """5. Concurrent workers do not activate conflicting proposals for the same student/bed."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="overlap")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="ov")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="ov")
    u3, r3, b3, occ3 = _create_student_with_bed(inst, hostel, floor, 3, prefix="ov")

    # u1 can swap with u2 (2-way)
    # OR u1 -> u2 -> u3 -> u1 (3-way)
    # Candidate selection must select at most 1 cycle so no student is double booked
    req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "want b2"})
    req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "want b1"})
    req3 = create_exchange_request(student=u3, data={"preferred_bed": b1, "reason": "want b1"})

    job = run_institution_matching(institution_id=inst.id)
    assert job.status == MatchingJob.Status.COMPLETED

    # Verify no student is reserved more than once
    active_reservations = ProposalParticipant.objects.filter(
        proposal__institution=inst,
        is_active_reservation=True,
    )
    student_ids = [p.student_id for p in active_reservations]
    assert len(student_ids) == len(set(student_ids)), "A student was reserved in multiple active proposals!"


@pytest.mark.django_db
def test_preference_change_during_matching_invalidates_stale_candidate():
    """6. Preference change during matching invalidates stale candidate."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="prefchg")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="pc")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="pc")
    u3, r3, b3, occ3 = _create_student_with_bed(inst, hostel, floor, 3, prefix="pc")

    req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "want b2"})
    req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "want b1"})

    # Student 1 updates preferences to Bed 3 instead of Bed 2!
    update_exchange_request(
        exchange_request=req1,
        student=u1,
        data={"preferred_bed": b3, "reason": "changed mind, want b3"},
    )

    # Background matching runs
    job = run_institution_matching(institution_id=inst.id)
    assert job.status == MatchingJob.Status.COMPLETED
    # (req1 <-> req2) is no longer valid because req1 targets b3, not b2
    assert job.proposals_activated_count == 0

    req1.refresh_from_db()
    req2.refresh_from_db()
    assert req1.status == ExchangeRequest.Status.OPEN
    assert req2.status == ExchangeRequest.Status.OPEN


@pytest.mark.django_db
def test_request_withdrawal_or_expiry_prevents_activation():
    """7. Request withdrawal or expiry prevents activation."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="withdr")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="wd")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="wd")

    req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "want b2"})
    req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "want b1"})

    # Student 1 withdraws
    req1 = withdraw_exchange_request(exchange_request=req1, student=u1)
    req1.refresh_from_db()
    assert req1.status == ExchangeRequest.Status.WITHDRAWN

    job = run_institution_matching(institution_id=inst.id)
    assert job.status == MatchingJob.Status.COMPLETED
    assert job.proposals_activated_count == 0


@pytest.mark.django_db
def test_declined_proposal_suppresses_cycle_from_immediate_reactivation():
    """8. Declined proposal suppresses that cycle from immediate reactivation."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="decl")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="dc")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="dc")

    req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "want b2"})
    req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "want b1"})

    # Match and activate
    run_institution_matching(institution_id=inst.id)
    prop = ExchangeProposal.objects.filter(institution=inst, cycle_length=2).latest("created_at")
    assert prop.status == ExchangeProposal.Status.PENDING_ACCEPTANCE

    # Student 2 declines
    respond_to_proposal(proposal_id=prop.id, student=u2, action="decline")
    prop.refresh_from_db()
    assert prop.status == ExchangeProposal.Status.CANCELLED

    # Check suppression record created
    supp = SuppressedCycle.objects.filter(institution=inst, cycle_signature=prop.cycle_signature).first()
    assert supp is not None
    assert supp.is_active is True
    assert supp.reason == "declined"

    # Immediately run matching again
    job2 = run_institution_matching(institution_id=inst.id)
    # The suppressed cycle must NOT be re-activated!
    assert job2.proposals_activated_count == 0

    req1.refresh_from_db()
    req2.refresh_from_db()
    assert req1.status == ExchangeRequest.Status.OPEN
    assert req1.matching_status == ExchangeRequest.MatchingStatus.NO_MATCH


@pytest.mark.django_db
def test_updating_preferences_unsuppresses_previously_declined_cycle():
    """9. Updating preferences unsuppresses previously declined cycle."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="unsupp")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="us")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="us")

    req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "want b2"})
    req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "want b1"})

    run_institution_matching(institution_id=inst.id)
    prop = ExchangeProposal.objects.filter(institution=inst, cycle_length=2).latest("created_at")

    # Student 2 declines -> cycle suppressed
    respond_to_proposal(proposal_id=prop.id, student=u2, action="decline")
    supp = SuppressedCycle.objects.get(institution=inst, cycle_signature=prop.cycle_signature)
    assert supp.is_active is True

    # Advance time slightly and Student 2 modifies their reason/preference
    future_time = timezone.now() + timedelta(minutes=5)
    update_exchange_request(
        exchange_request=req2,
        student=u2,
        data={"preferred_bed": b1, "reason": "Now I want to swap again!"},
        now=future_time,
    )

    # Now run matching again -> suppression should be lifted!
    job = run_institution_matching(institution_id=inst.id, now=future_time)
    assert job.proposals_activated_count == 1

    supp.refresh_from_db()
    assert supp.is_active is False


@pytest.mark.django_db
def test_broker_failure_leaves_durable_outbox_that_reconciliation_recovers():
    """10. Broker failure leaves durable outbox record that reconciliation recovers."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="brk")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="bk")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="bk")

    # Mock Celery delay to simulate broker outage
    with patch("apps.exchange.tasks.run_institution_matching_task.delay", side_effect=Exception("Redis connection refused")):
        req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "b2"})
        req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "b1"})

    # Even though Celery failed, outbox entries exist and are unprocessed
    unprocessed = MatchingOutbox.objects.filter(institution=inst, processed=False)
    assert unprocessed.count() >= 2

    # Reconciliation runs
    scheduled = reconcile_all_institutions_matching()
    assert inst.id in scheduled

    # Process matching
    job = run_institution_matching(institution_id=inst.id)
    assert job.proposals_activated_count == 1

    # Outbox is now marked processed
    assert MatchingOutbox.objects.filter(institution=inst, processed=False).count() == 0


@pytest.mark.django_db
def test_vacant_bed_transfers_never_enter_matching():
    """11. Vacant-bed transfers never enter matching."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="vct")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="vt")

    # Create a vacant bed
    r_vacant = Room.objects.create(
        floor=floor,
        room_number="199",
        room_type=Room.RoomType.SINGLE,
        operational_status=OperationalStatus.IN_SERVICE,
    )
    b_vacant = Bed.objects.create(
        room=r_vacant,
        bed_label="Bed V",
        operational_status=OperationalStatus.IN_SERVICE,
    )

    # Student 1 selects vacant bed -> creates VACANT_TRANSFER request
    vt_req = create_exchange_request(
        student=u1,
        data={"preferred_bed": b_vacant, "reason": "Move to vacant single room"},
    )
    assert vt_req.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER
    assert vt_req.status == ExchangeRequest.Status.PENDING_APPROVAL

    # Run matching worker
    job = run_institution_matching(institution_id=inst.id)
    assert job.status == MatchingJob.Status.COMPLETED
    assert job.candidates_found_count == 0
    assert job.proposals_activated_count == 0

    vt_req.refresh_from_db()
    # Status remains untouched by matching engine
    assert vt_req.status == ExchangeRequest.Status.PENDING_APPROVAL


@pytest.mark.django_db
def test_institutional_isolation_is_strictly_preserved():
    """12. Institutional isolation is strictly preserved."""
    inst1 = Institution.objects.create(name="Apex University", code="APEX1")
    inst2 = Institution.objects.create(name="Zenith University", code="ZENITH")
    _configure_policies(inst1)
    _configure_policies(inst2)

    h1, blk1, fl1, w1 = _setup_hostel_env(inst1, prefix="iso1")
    h2, blk2, fl2, w2 = _setup_hostel_env(inst2, prefix="iso2")

    u1_a, r1_a, b1_a, occ1_a = _create_student_with_bed(inst1, h1, fl1, 1, prefix="i1a")
    u1_b, r1_b, b1_b, occ1_b = _create_student_with_bed(inst1, h1, fl1, 2, prefix="i1b")

    u2_a, r2_a, b2_a, occ2_a = _create_student_with_bed(inst2, h2, fl2, 1, prefix="i2a")
    u2_b, r2_b, b2_b, occ2_b = _create_student_with_bed(inst2, h2, fl2, 2, prefix="i2b")

    # Cross-institution request creation is blocked by validation
    with pytest.raises(ValidationError) as exc_info:
        create_exchange_request(student=u1_a, data={"preferred_bed": b2_a, "reason": "Cross inst"})
    assert any("Preferred bed must belong to your institution" in msg for msg in exc_info.value.messages)

    # Valid intra-institution requests in both institutions
    req1_a = create_exchange_request(student=u1_a, data={"preferred_bed": b1_b, "reason": "Inst1 swap"})
    req1_b = create_exchange_request(student=u1_b, data={"preferred_bed": b1_a, "reason": "Inst1 swap"})

    req2_a = create_exchange_request(student=u2_a, data={"preferred_bed": b2_b, "reason": "Inst2 swap"})
    req2_b = create_exchange_request(student=u2_b, data={"preferred_bed": b2_a, "reason": "Inst2 swap"})

    # Run matching for Inst 1 ONLY
    job1 = run_institution_matching(institution_id=inst1.id)
    assert job1.proposals_activated_count == 1

    # Verify Inst 1 proposal exists, but Inst 2 remains completely untouched
    prop1 = ExchangeProposal.objects.get(institution=inst1)
    assert prop1.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    assert prop1.participants.count() == 2
    for p in prop1.participants.all():
        assert p.student.institution_id == inst1.id

    req2_a.refresh_from_db()
    req2_b.refresh_from_db()
    assert req2_a.status == ExchangeRequest.Status.OPEN
    assert req2_a.matching_status == ExchangeRequest.MatchingStatus.WAITING
    assert not ExchangeProposal.objects.filter(institution=inst2).exists()


@pytest.mark.django_db
def test_matching_and_activation_never_mutate_physical_occupancy():
    """13. Matching and activation never mutate physical occupancy."""
    inst = Institution.objects.create(name="Apex University", code="APEX")
    _configure_policies(inst)
    hostel, block, floor, warden = _setup_hostel_env(inst, prefix="nomut")

    u1, r1, b1, occ1 = _create_student_with_bed(inst, hostel, floor, 1, prefix="nm1")
    u2, r2, b2, occ2 = _create_student_with_bed(inst, hostel, floor, 2, prefix="nm2")

    req1 = create_exchange_request(student=u1, data={"preferred_bed": b2, "reason": "want b2"})
    req2 = create_exchange_request(student=u2, data={"preferred_bed": b1, "reason": "want b1"})

    job = run_institution_matching(institution_id=inst.id)
    assert job.proposals_activated_count == 1

    # Check physical occupancies
    occ1.refresh_from_db()
    occ2.refresh_from_db()
    assert occ1.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ1.bed_id == b1.id
    assert occ2.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occ2.bed_id == b2.id
