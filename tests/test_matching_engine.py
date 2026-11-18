from datetime import timedelta
import io
import json
from time import perf_counter
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_STUDENT,
    create_user_with_role,
    ensure_roles_exist,
)
from apps.core.models import Institution
from apps.exchange.eligibility import ExternalStatusResult
from apps.exchange.matching import (
    ExchangeGraph,
    build_candidate_matches,
    build_exchange_graph,
    find_candidate_matches,
    find_exchange_cycles,
    persist_candidate_matches,
)
from apps.exchange.models import (
    ExchangeProposal,
    ExchangeRequest,
    PolicyRule,
    ProposalParticipant,
)
from apps.exchange.services import (
    create_exchange_request as _orig_create_exchange_request,
    run_exchange_matching,
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


class MockStatusAdapter:
    def __init__(self, fee_result: ExternalStatusResult, disciplinary_result: ExternalStatusResult):
        self._fee = fee_result
        self._disciplinary = disciplinary_result

    def check_fee_status(self, student) -> ExternalStatusResult:
        return self._fee

    def check_disciplinary_status(self, student) -> ExternalStatusResult:
        return self._disciplinary


def _configure_policies(institution, **kwargs):
    defaults = {
        PolicyRule.Key.COOLING_OFF_DAYS: 0,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
    }
    defaults.update(kwargs)
    for key, val in defaults.items():
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


def _create_cycle_users_and_rooms(
    count,
    institution,
    warden_user,
    hostel=None,
    gender=User.ResidentialEligibility.MALE,
    prefix="test",
):
    """Helper to create `count` students and distinct rooms in `hostel`."""
    ensure_roles_exist()
    if hostel is None:
        hostel = Hostel.objects.create(
            institution=institution,
            name=f"Hostel {prefix}",
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
        bed = Bed.objects.create(room=room, bed_label="Bed A", operational_status=OperationalStatus.IN_SERVICE)
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
        "block": block,
        "floor": floor,
        "users": users,
        "rooms": rooms,
        "beds": beds,
        "occupancies": occupancies,
    }


# =========================================================================
# 1. Valid 2-way, 3-way, 4-way, and 5-way Cycle Discovery
# =========================================================================

@pytest.mark.django_db
@pytest.mark.parametrize("cycle_length", [2, 3, 4, 5])
def test_valid_cycles_2_to_5_participants(
    cycle_length,
    institution,
    warden_user,
):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=cycle_length,
        institution=institution,
        warden_user=warden_user,
        prefix=f"cyc{cycle_length}",
    )
    users = setup["users"]
    beds = setup["beds"]

    # Form a directed cycle where user[i] wants user[(i+1)%k]'s bed
    for i in range(cycle_length):
        target_bed = beds[(i + 1) % cycle_length]
        create_exchange_request(
            student=users[i],
            data={
                "preferred_hostel": setup["hostel"],
                "preferred_bed": target_bed,
                "reason": f"Participant {i} wants bed of participant {(i + 1) % cycle_length}",
            },
            direct_invitation=False,
        )

    result = find_candidate_matches(institution=institution)
    assert result.metrics.total_candidates == 1
    assert result.metrics.candidates_by_length.get(cycle_length) == 1

    candidate = result.candidates[0]
    assert candidate.cycle_length == cycle_length
    assert not candidate.is_cross_hostel
    assert candidate.classification == "same_hostel"
    assert len(candidate.participants) == cycle_length

    # Verify each participant's assignment in cyclic rotation order
    part_current_beds = {p.current_bed_id for p in candidate.participants}
    assert part_current_beds == {b.id for b in beds}

    for i in range(cycle_length):
        p = candidate.participants[i]
        next_p = candidate.participants[(i + 1) % cycle_length]
        assert p.proposed_bed_id == next_p.current_bed_id
        assert "Target bed" in p.preference_match_explanation


# =========================================================================
# 2. Exclusion of 6-way Cycles
# =========================================================================

@pytest.mark.django_db
def test_exclusion_of_6_way_cycles(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=6,
        institution=institution,
        warden_user=warden_user,
        prefix="sixway",
    )
    users = setup["users"]
    beds = setup["beds"]

    # Form a strict 6-cycle: 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 0
    for i in range(6):
        target_bed = beds[(i + 1) % 6]
        create_exchange_request(
            student=users[i],
            data={"preferred_bed": target_bed, "reason": "6-cycle test"},
        )

    result = find_candidate_matches(institution=institution, max_length=5)
    # The 6-cycle must be strictly excluded
    assert result.metrics.total_candidates == 0
    assert len(result.candidates) == 0


# =========================================================================
# 3. One-Way Preferences Produce No Match
# =========================================================================

@pytest.mark.django_db
def test_one_way_preferences_produce_no_match(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=3,
        institution=institution,
        warden_user=warden_user,
        prefix="oneway",
    )
    users = setup["users"]
    beds = setup["beds"]

    # 0 wants 1, 1 wants 2, but 2 wants an unallocated or unrelated room (no closure to 0)
    create_exchange_request(student=users[0], data={"preferred_bed": beds[1], "reason": "wants 1"})
    create_exchange_request(student=users[1], data={"preferred_bed": beds[2], "reason": "wants 2"})
    # Student 2 has general preference for a single room that does not match 0's double room
    create_exchange_request(
        student=users[2],
        data={"preferred_hostel": setup["hostel"], "preferred_room_type": Room.RoomType.SINGLE, "reason": "wants single"},
    )

    result = find_candidate_matches(institution=institution)
    assert result.metrics.total_candidates == 0


# =========================================================================
# 4. Duplicate Cycle Prevention & Distinct Directed Cycles
# =========================================================================

@pytest.mark.django_db
def test_duplicate_cycle_prevention_under_rotation(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=3,
        institution=institution,
        warden_user=warden_user,
        prefix="dedup",
    )
    users = setup["users"]
    beds = setup["beds"]

    # 0 -> 1 -> 2 -> 0
    for i in range(3):
        create_exchange_request(
            student=users[i],
            data={"preferred_bed": beds[(i + 1) % 3], "reason": "dedup test"},
        )

    result = find_candidate_matches(institution=institution)
    # Must find exactly 1 candidate, NOT 3 rotations
    assert result.metrics.total_candidates == 1
    assert len(result.candidates) == 1


@pytest.mark.django_db
def test_distinct_directed_cycles_remain_distinct(institution, warden_user):
    """
    When students have general preferences allowing cycles in both directions:
    A -> B -> C -> A AND A -> C -> B -> A.
    Both distinct directed cycles must be discovered and not conflated.
    """
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=3,
        institution=institution,
        warden_user=warden_user,
        prefix="bidir",
    )
    users = setup["users"]

    # All 3 students accept any double room on floor 1 in this hostel
    for i in range(3):
        create_exchange_request(
            student=users[i],
            data={
                "preferred_hostel": setup["hostel"],
                "preferred_floor": setup["floor"],
                "preferred_room_type": Room.RoomType.DOUBLE,
                "reason": "Flexible accommodation in same block",
            },
        )

    result = find_candidate_matches(institution=institution)
    signatures = {c.cycle_signature for c in result.candidates}

    assert len(signatures) == len(result.candidates)
    three_way_candidates = [c for c in result.candidates if c.cycle_length == 3]
    # Two distinct directed assignments exist over the 3 participants: clockwise and counter-clockwise
    assert len(three_way_candidates) == 2
    assert three_way_candidates[0].cycle_signature != three_way_candidates[1].cycle_signature


# =========================================================================
# 5. Excluded Requests: Expired, Withdrawn, and Ineligible
# =========================================================================

@pytest.mark.django_db
def test_expired_and_withdrawn_requests_excluded(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=2,
        institution=institution,
        warden_user=warden_user,
        prefix="expwith",
    )
    users = setup["users"]
    beds = setup["beds"]

    req0 = create_exchange_request(student=users[0], data={"preferred_bed": beds[1], "reason": "r0"})
    req1 = create_exchange_request(student=users[1], data={"preferred_bed": beds[0], "reason": "r1"})

    # Case 1: req0 is expired
    now = timezone.now()
    ExchangeRequest.objects.filter(pk=req0.pk).update(
        created_at=now - timedelta(days=35),
        expires_at=now - timedelta(days=1),
    )
    res_expired = find_candidate_matches(institution=institution, now=now)
    assert res_expired.metrics.total_candidates == 0

    # Reset expiry, but mark req1 as withdrawn
    ExchangeRequest.objects.filter(pk=req0.pk).update(expires_at=now + timedelta(days=10))
    ExchangeRequest.objects.filter(pk=req1.pk).update(status=ExchangeRequest.Status.WITHDRAWN)
    res_withdrawn = find_candidate_matches(institution=institution, now=now)
    assert res_withdrawn.metrics.total_candidates == 0


@pytest.mark.django_db
def test_cooling_off_and_external_holds_exclude_requests(institution, warden_user):
    setup = _create_cycle_users_and_rooms(
        count=2,
        institution=institution,
        warden_user=warden_user,
        prefix="holds",
    )
    users = setup["users"]
    beds = setup["beds"]

    # 1. Cooling-off hold: 14 days
    _configure_policies(institution, cooling_off_days=14)
    # Student allocated today, so cannot submit with today date, simulate request submitted in future
    future_now = timezone.now() + timedelta(days=15)
    create_exchange_request(
        student=users[0],
        data={"preferred_bed": beds[1], "reason": "holds test"},
        now=future_now,
    )
    create_exchange_request(
        student=users[1],
        data={"preferred_bed": beds[0], "reason": "holds test"},
        now=future_now,
    )

    # Evaluate matching on day 2 (under cooling off)
    res_cooling = find_candidate_matches(institution=institution, now=timezone.now() + timedelta(days=2))
    assert res_cooling.metrics.total_candidates == 0

    # Evaluate matching on day 16 (cooling off passed)
    res_cooling_passed = find_candidate_matches(institution=institution, now=timezone.now() + timedelta(days=16))
    assert res_cooling_passed.metrics.total_candidates == 1

    # 2. Fee hold
    _configure_policies(institution, cooling_off_days=0, require_fee_clearance=True)
    hold_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=False, reason="Unpaid fee", status="blocked"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
    )
    res_fee = find_candidate_matches(institution=institution, status_adapter=hold_adapter)
    assert res_fee.metrics.total_candidates == 0

    # Cleared adapter passes
    cleared_adapter = MockStatusAdapter(
        fee_result=ExternalStatusResult(available=True, eligible=True, reason="All fees clear", status="cleared"),
        disciplinary_result=ExternalStatusResult(available=True, eligible=True, status="cleared"),
    )
    res_cleared = find_candidate_matches(institution=institution, status_adapter=cleared_adapter)
    assert res_cleared.metrics.total_candidates == 1


# =========================================================================
# 6. Changed or Missing Occupancy Excluded
# =========================================================================

@pytest.mark.django_db
def test_changed_or_missing_occupancy_excluded(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=2,
        institution=institution,
        warden_user=warden_user,
        prefix="occexcl",
    )
    users = setup["users"]
    beds = setup["beds"]
    occ0 = setup["occupancies"][0]

    create_exchange_request(student=users[0], data={"preferred_bed": beds[1], "reason": "test"})
    create_exchange_request(student=users[1], data={"preferred_bed": beds[0], "reason": "test"})

    # Student 0's occupancy is cancelled / released
    occ0.status = StudentOccupancy.OccupancyStatus.CANCELLED
    occ0.save(update_fields=["status"])

    result = find_candidate_matches(institution=institution)
    assert result.metrics.total_candidates == 0


# =========================================================================
# 7. Institution and Gender-Block Isolation
# =========================================================================

@pytest.mark.django_db
def test_institution_isolation(institution, other_institution, warden_user):
    _configure_policies(institution)
    _configure_policies(other_institution)

    # Setup in institution A
    setup_a = _create_cycle_users_and_rooms(count=1, institution=institution, warden_user=warden_user, prefix="insta")
    # Setup in institution B
    setup_b = _create_cycle_users_and_rooms(count=1, institution=other_institution, warden_user=warden_user, prefix="instb")

    u_a = setup_a["users"][0]
    u_b = setup_b["users"][0]

    # Student A requests Double room in Institution A
    create_exchange_request(student=u_a, data={"preferred_room_type": Room.RoomType.DOUBLE, "reason": "instA"})
    # Student B requests Double room in Institution B
    create_exchange_request(student=u_b, data={"preferred_room_type": Room.RoomType.DOUBLE, "reason": "instB"})

    # In matching for institution A, student B from institution B cannot form a cycle
    res_a = find_candidate_matches(institution=institution)
    assert res_a.metrics.total_candidates == 0

    res_b = find_candidate_matches(institution=other_institution)
    assert res_b.metrics.total_candidates == 0


@pytest.mark.django_db
def test_gender_block_isolation(institution, warden_user):
    _configure_policies(institution)

    # Male student in male hostel
    setup_male = _create_cycle_users_and_rooms(
        count=1,
        institution=institution,
        warden_user=warden_user,
        gender=User.ResidentialEligibility.MALE,
        prefix="malegroup",
    )
    # Female student in female hostel
    setup_female = _create_cycle_users_and_rooms(
        count=1,
        institution=institution,
        warden_user=warden_user,
        gender=User.ResidentialEligibility.FEMALE,
        prefix="femalegroup",
    )

    u_m = setup_male["users"][0]
    u_f = setup_female["users"][0]

    # Male student requests Double room, Female student requests Double room
    create_exchange_request(student=u_m, data={"preferred_room_type": Room.RoomType.DOUBLE, "reason": "male student"})
    create_exchange_request(student=u_f, data={"preferred_room_type": Room.RoomType.DOUBLE, "reason": "female student"})

    res = find_candidate_matches(institution=institution)
    # No edges between male and female hostels: 0 matches
    assert res.metrics.total_candidates == 0


# =========================================================================
# 8. Existing Active-Proposal Conflicts
# =========================================================================

@pytest.mark.django_db
def test_existing_active_proposal_reservation_excludes_request(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=2,
        institution=institution,
        warden_user=warden_user,
        prefix="actprop",
    )
    users = setup["users"]
    beds = setup["beds"]

    req0 = create_exchange_request(student=users[0], data={"preferred_bed": beds[1], "reason": "r0"})
    create_exchange_request(student=users[1], data={"preferred_bed": beds[0], "reason": "r1"})

    # Lock req0 in an existing active proposal reservation
    proposal = ExchangeProposal.objects.create(
        institution=institution,
        status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
        cycle_length=2,
    )
    ProposalParticipant.objects.create(
        proposal=proposal,
        exchange_request=req0,
        student=users[0],
        current_occupancy=setup["occupancies"][0],
        proposed_bed=beds[1],
        sequence_order=0,
        is_active_reservation=True,
    )

    # req0 is actively reserved, so it must be excluded from candidate matching input
    result = find_candidate_matches(institution=institution)
    assert result.metrics.total_candidates == 0


# =========================================================================
# 9. Repeated Runs & Persistence Idempotency
# =========================================================================

@pytest.mark.django_db
def test_repeated_runs_and_safe_idempotent_persistence(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=2,
        institution=institution,
        warden_user=warden_user,
        prefix="repeat",
    )
    users = setup["users"]
    beds = setup["beds"]

    create_exchange_request(student=users[0], data={"preferred_bed": beds[1], "reason": "rep0"})
    create_exchange_request(student=users[1], data={"preferred_bed": beds[0], "reason": "rep1"})

    # Run 1 with persist
    res1 = run_exchange_matching(institution=institution, persist_candidates=True)
    assert res1.metrics.total_candidates == 1
    assert len(res1.proposals_created) == 1

    first_proposal_id = res1.proposals_created[0].id
    prop = ExchangeProposal.objects.get(id=first_proposal_id)
    assert prop.status == ExchangeProposal.Status.DRAFT
    # Non-locking reservations
    assert all(not p.is_active_reservation for p in prop.participants.all())

    # Run 2 with persist: must NOT duplicate draft proposals
    res2 = run_exchange_matching(institution=institution, persist_candidates=True)
    assert res2.metrics.total_candidates == 1
    assert len(res2.proposals_created) == 0

    assert ExchangeProposal.objects.filter(institution=institution).count() == 1


# =========================================================================
# 10. Zero Occupancy Mutations Invariance
# =========================================================================

@pytest.mark.django_db
def test_zero_occupancy_mutations_across_candidate_matching(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=3,
        institution=institution,
        warden_user=warden_user,
        prefix="invar",
    )
    users = setup["users"]
    beds = setup["beds"]
    occupancies = setup["occupancies"]

    initial_state = [(occ.id, occ.bed_id, occ.status, occ.start_date) for occ in occupancies]

    for i in range(3):
        create_exchange_request(
            student=users[i],
            data={"preferred_bed": beds[(i + 1) % 3], "reason": "invariance test"},
        )

    # Run matching and candidate persistence
    res = run_exchange_matching(institution=institution, persist_candidates=True)
    assert res.metrics.total_candidates == 1

    # Verify zero occupancy changes
    for idx, occ in enumerate(occupancies):
        occ.refresh_from_db()
        expected = initial_state[idx]
        assert (occ.id, occ.bed_id, occ.status, occ.start_date) == expected

    # Verify requests remain OPEN
    for req in ExchangeRequest.objects.filter(institution=institution):
        assert req.status == ExchangeRequest.Status.OPEN


# =========================================================================
# 11. Performance Benchmark: 2,000 Open Requests in Under 5 Seconds
# =========================================================================

@pytest.mark.django_db
def test_performance_benchmark_2000_open_requests_under_five_seconds():
    """
    Simulates 2,000 open eligible requests with realistic sparse preference patterns.
    Measures data loading, graph building, and cycle detection times separately.
    Asserts total elapsed time < 5.0 seconds.
    """
    now = timezone.now()
    requests = []
    hostel = SimpleNamespace(id=1, institution_id=1, status="active", resident_category="male", name="Hostel A")
    block = SimpleNamespace(id=1, hostel=hostel, is_active=True, name="Block A")
    floor = SimpleNamespace(id=1, block=block, is_active=True, floor_label="1F")

    for i in range(2000):
        room = SimpleNamespace(
            floor=floor,
            is_active=True,
            operational_status="in_service",
            room_type="double",
            occupancy_category="Standard",
            accessibility_attributes="",
            room_number=f"{100 + (i % 200)}",
        )
        bed = SimpleNamespace(
            id=i,
            room=room,
            is_active=True,
            operational_status="in_service",
            bed_label="Bed A",
        )
        student = SimpleNamespace(
            id=i,
            residential_eligibility="male",
            full_name=f"Student {i}",
            registration_number=f"REG-{i:04d}",
        )
        occupancy = SimpleNamespace(
            status="active",
            student_id=i,
            institution_id=1,
            bed_id=i,
            bed=bed,
            id=i,
            start_date=now.date() - timedelta(days=20),
        )
        target_bed_id = (i + 1) if (i % 2 == 0) else (i - 1)
        requests.append(
            SimpleNamespace(
                id=f"req-{i}",
                status="open",
                expires_at=now + timedelta(days=14),
                institution_id=1,
                student=student,
                student_id=i,
                current_occupancy=occupancy,
                preferred_hostel_id=1,
                preferred_bed_id=target_bed_id,
                preferred_block_id=None,
                preferred_floor_id=None,
                preferred_room_type="double",
                preferred_occupancy_category="Standard",
                preferred_accessibility_attributes="",
                reason="Performance test request",
            )
        )

    t_start = perf_counter()
    graph = build_exchange_graph(requests=requests, now=now)
    t_graph = perf_counter() - t_start

    t_cycles_start = perf_counter()
    cycles = find_exchange_cycles(graph, min_length=2, max_length=5)
    t_cycles = perf_counter() - t_cycles_start

    t_total = perf_counter() - t_start

    assert len(graph.vertices) == 2000
    assert sum(len(edges) for edges in graph.edges.values()) == 2000
    assert len(cycles) == 1000
    assert not getattr(cycles, "limit_reached", False)

    # Benchmark assertions
    assert t_total < 5.0, f"Matching 2000 requests took {t_total:.3f}s (exceeds 5.0s limit)"
    assert t_graph >= 0.0
    assert t_cycles >= 0.0


# =========================================================================
# 12. Management Command Execution
# =========================================================================

@pytest.mark.django_db
def test_match_exchanges_management_command(institution, warden_user):
    _configure_policies(institution)
    setup = _create_cycle_users_and_rooms(
        count=4,
        institution=institution,
        warden_user=warden_user,
        prefix="cmdtest",
    )
    users = setup["users"]
    beds = setup["beds"]

    for i in range(4):
        create_exchange_request(
            student=users[i],
            data={"preferred_bed": beds[(i + 1) % 4], "reason": f"cmd rotation {i}"},
        )

    # 1. Text table output
    out = io.StringIO()
    call_command("match_exchanges", institution=institution.code, stdout=out)
    output = out.getvalue()

    assert "ROOMLOOP MATCHING ENGINE" in output
    assert "Open Eligible Requests (Graph Nodes) : 4" in output
    assert "Total Candidate Matches Found        : 1" in output
    assert "4-way cycles                     : 1" in output
    assert "Complete Exhaustive Search           : Yes" in output

    # 2. JSON output
    out_json = io.StringIO()
    call_command("match_exchanges", institution=institution.code, format="json", stdout=out_json)
    data = json.loads(out_json.getvalue())

    assert data["institution"]["code"] == institution.code
    assert data["metrics"]["graph_nodes"] == 4
    assert data["metrics"]["total_candidates"] == 1
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["cycle_length"] == 4
    assert len(data["candidates"][0]["participants"]) == 4

    # 3. Persist option
    out_persist = io.StringIO()
    call_command("match_exchanges", institution=institution.code, persist=True, stdout=out_persist)
    persist_output = out_persist.getvalue()
    assert "Persisted Draft Proposals            : 1 created" in persist_output
    assert ExchangeProposal.objects.filter(institution=institution, status=ExchangeProposal.Status.DRAFT).count() == 1
