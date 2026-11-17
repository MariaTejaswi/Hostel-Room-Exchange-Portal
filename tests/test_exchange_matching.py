from types import SimpleNamespace
from time import perf_counter

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import ROLE_STUDENT, create_user_with_role
from apps.exchange.matching import (
    build_exchange_graph,
    create_exchange_proposals,
    find_exchange_cycles,
)
from apps.exchange.models import ExchangeProposal, ExchangeRequest
from apps.hostels.models import Bed, Room
from apps.hostels.services import assign_student_to_bed


def _request_data(hostel, block, floor, room):
    return {
        "preferred_hostel": hostel,
        "preferred_block": block,
        "preferred_floor": floor,
        "preferred_room_type": room.room_type,
        "preferred_occupancy_category": room.occupancy_category,
        "preferred_accessibility_attributes": room.accessibility_attributes,
        "reason": "I need a compatible room exchange for my accommodation.",
    }


def _configure_policies(institution):
    from apps.exchange.models import PolicyRule

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


def _make_cycle(
    *,
    count,
    institution,
    student_user,
    roles,
    hostel_setup,
    warden_user,
):
    _configure_policies(institution)
    users = [student_user]
    for index in range(1, count):
        users.append(
            create_user_with_role(
                email=f"cycle-{count}-{index}@apex.test",
                full_name=f"Cycle Student {count}-{index}",
                role_name=ROLE_STUDENT,
                institution=institution,
                password="TestPassword123!",
                registration_number=f"CYCLE-{count}-{index}",
                residential_eligibility=User.ResidentialEligibility.MALE,
            )
        )

    rooms = []
    occupancies = []
    for index, user in enumerate(users):
        room = Room.objects.create(
            floor=hostel_setup["floor"],
            room_number=f"{count}{index:02d}",
            room_type=Room.RoomType.DOUBLE,
            occupancy_category="Standard",
            accessibility_attributes=f"cycle-room-{count}-{index}",
        )
        bed = Bed.objects.create(room=room, bed_label="Bed A")
        rooms.append(room)
        occupancies.append(
            assign_student_to_bed(
                student=user,
                bed=bed,
                created_by=warden_user,
            )
        )

    requests = []
    for index, user in enumerate(users):
        target_room = rooms[(index + 1) % count]
        requests.append(
            ExchangeRequest.objects.get(
                id=create_request_for_user(
                    user=user,
                    institution=institution,
                    occupancy=occupancies[index],
                    data=_request_data(
                        hostel=hostel_setup["hostel"],
                        block=hostel_setup["block"],
                        floor=hostel_setup["floor"],
                        room=target_room,
                    ),
                ).id
            )
        )
    return requests, rooms


def create_request_for_user(*, user, institution, occupancy, data):
    from apps.exchange.services import create_exchange_request

    return create_exchange_request(student=user, data=data)


@pytest.mark.django_db
@pytest.mark.parametrize("cycle_length", [2, 3, 4, 5])
def test_matching_detects_and_creates_cycles_of_supported_lengths(
    cycle_length,
    institution,
    student_user,
    roles,
    hostel_setup,
    warden_user,
):
    requests, rooms = _make_cycle(
        count=cycle_length,
        institution=institution,
        student_user=student_user,
        roles=roles,
        hostel_setup=hostel_setup,
        warden_user=warden_user,
    )

    graph = build_exchange_graph(institution=institution)
    cycles = find_exchange_cycles(graph)

    assert [len(cycle.request_ids) for cycle in cycles] == [cycle_length]
    proposals = create_exchange_proposals(institution=institution)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
    assert proposal.cycle_length == cycle_length
    assert proposal.participants.count() == cycle_length
    assert ExchangeRequest.objects.filter(
        id__in=[request.id for request in requests],
        status=ExchangeRequest.Status.MATCHED,
    ).count() == cycle_length

    request_by_id = {str(request.id): request for request in requests}
    ordered_request_ids = list(
        proposal.participants.order_by("sequence_order").values_list(
            "exchange_request_id",
            flat=True,
        )
    )
    expected_bed_ids = [
        request_by_id[str(ordered_request_ids[(index + 1) % cycle_length])]
        .current_occupancy.bed_id
        for index in range(cycle_length)
    ]
    participant_bed_ids = list(
        proposal.participants.order_by("sequence_order").values_list(
            "proposed_bed_id",
            flat=True,
        )
    )
    assert participant_bed_ids == expected_bed_ids


@pytest.mark.django_db
def test_incompatible_preferences_produce_no_cycle(
    institution,
    student_user,
    roles,
    hostel_setup,
    warden_user,
):
    requests, _ = _make_cycle(
        count=3,
        institution=institution,
        student_user=student_user,
        roles=roles,
        hostel_setup=hostel_setup,
        warden_user=warden_user,
    )
    requests[1].preferred_accessibility_attributes = "does-not-exist"
    requests[1].save(update_fields=["preferred_accessibility_attributes"])

    graph = build_exchange_graph(institution=institution)

    assert find_exchange_cycles(graph) == []
    assert create_exchange_proposals(institution=institution) == []


@pytest.mark.django_db
def test_duplicate_matching_does_not_create_another_active_proposal(
    institution,
    student_user,
    roles,
    hostel_setup,
    warden_user,
):
    _make_cycle(
        count=2,
        institution=institution,
        student_user=student_user,
        roles=roles,
        hostel_setup=hostel_setup,
        warden_user=warden_user,
    )

    first = create_exchange_proposals(institution=institution)
    second = create_exchange_proposals(institution=institution)

    assert len(first) == 1
    assert second == []
    assert ExchangeProposal.objects.filter(
        status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
    ).count() == 1


@pytest.mark.django_db
def test_matching_revalidates_eligibility_before_creating_proposal(
    institution,
    student_user,
    roles,
    hostel_setup,
    warden_user,
):
    _make_cycle(
        count=2,
        institution=institution,
        student_user=student_user,
        roles=roles,
        hostel_setup=hostel_setup,
        warden_user=warden_user,
    )
    from apps.exchange.models import PolicyRule

    graph = build_exchange_graph(institution=institution)
    PolicyRule.objects.filter(
        institution=institution,
        key=PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
    ).update(value={"value": True})

    assert find_exchange_cycles(graph)
    assert create_exchange_proposals(institution=institution) == []
    assert ExchangeRequest.objects.filter(
        status=ExchangeRequest.Status.OPEN,
    ).count() == 2


@pytest.mark.django_db
def test_graph_building_handles_2000_requests_under_five_seconds():
    now = timezone.now()
    requests = []
    for index in range(2000):
        hostel = SimpleNamespace(
            id=1,
            institution_id=1,
            status="active",
            resident_category="male",
        )
        block = SimpleNamespace(id=1, hostel=hostel, is_active=True)
        floor = SimpleNamespace(id=1, block=block, is_active=True)
        room = SimpleNamespace(
            floor=floor,
            is_active=True,
            operational_status="in_service",
            room_type="double",
            occupancy_category="Standard",
            accessibility_attributes=f"[candidate-{index}]",
        )
        bed = SimpleNamespace(
            id=index,
            room=room,
            is_active=True,
            operational_status="in_service",
        )
        student = SimpleNamespace(
            residential_eligibility="male",
        )
        occupancy = SimpleNamespace(
            status="active",
            student_id=index,
            institution_id=1,
            bed_id=index,
            bed=bed,
        )
        requests.append(
            SimpleNamespace(
                id=index,
                status="open",
                expires_at=None,
                institution_id=1,
                student=student,
                student_id=index,
                current_occupancy=occupancy,
                preferred_hostel_id=None,
                preferred_bed_id=None,
                preferred_block_id=None,
                preferred_floor_id=None,
                preferred_room_type="double",
                preferred_occupancy_category="Standard",
                preferred_accessibility_attributes=f"[requested-{index}]",
            )
        )

    started = perf_counter()
    graph = build_exchange_graph(requests=requests, now=now)
    cycles = find_exchange_cycles(graph)
    elapsed = perf_counter() - started

    assert len(graph.vertices) == 2000
    assert sum(len(edges) for edges in graph.edges.values()) == 0
    assert cycles == []
    assert elapsed < 5
