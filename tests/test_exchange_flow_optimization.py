import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.exchange.models import ExchangeRequest, PolicyRule
from apps.exchange.selectors import (
    get_floor_room_summaries_for_exchange,
    get_room_bed_details_for_exchange,
)
from apps.hostels.models import Bed, OperationalStatus, Room
from apps.hostels.services import assign_student_to_bed


@pytest.fixture(autouse=True)
def configure_exchange_policies(institution):
    for key, val in [
        (PolicyRule.Key.COOLING_OFF_DAYS, 0),
        (PolicyRule.Key.REQUEST_VALIDITY_DAYS, 30),
        (PolicyRule.Key.REQUIRE_FEE_CLEARANCE, False),
        (PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD, False),
        (PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS, 48),
    ]:
        PolicyRule.objects.update_or_create(
            institution=institution,
            key=key,
            defaults={"value": {"value": val}, "is_active": True},
        )


@pytest.fixture
def multi_room_hostel_setup(hostel_setup):
    """Extend hostel_setup with an extra room, maintenance bed, and second floor."""
    floor = hostel_setup["floor"]
    room_2 = Room.objects.create(
        floor=floor,
        room_number="102",
        room_type=Room.RoomType.SINGLE,
        operational_status=OperationalStatus.IN_SERVICE,
    )
    bed_c = Bed.objects.create(
        room=room_2,
        bed_label="Bed C",
        operational_status=OperationalStatus.IN_SERVICE,
    )
    room_maint = Room.objects.create(
        floor=floor,
        room_number="103",
        room_type=Room.RoomType.DOUBLE,
        operational_status=OperationalStatus.MAINTENANCE,
    )
    bed_m1 = Bed.objects.create(
        room=room_maint,
        bed_label="Bed M1",
        operational_status=OperationalStatus.MAINTENANCE,
    )
    return {
        **hostel_setup,
        "room_2": room_2,
        "bed_c": bed_c,
        "room_maint": room_maint,
        "bed_m1": bed_m1,
    }


@pytest.mark.django_db
def test_floor_room_summaries_and_bed_counts(
    student_user,
    other_student_user,
    multi_room_hostel_setup,
    warden_user,
):
    # Occupy Bed A for current student
    assign_student_to_bed(
        student=student_user,
        bed=multi_room_hostel_setup["bed_a"],
        created_by=warden_user,
    )
    # Occupy Bed B for other student (swap target)
    assign_student_to_bed(
        student=other_student_user,
        bed=multi_room_hostel_setup["bed_b"],
        created_by=warden_user,
    )

    summaries = get_floor_room_summaries_for_exchange(
        student_user,
        multi_room_hostel_setup["floor"].id,
    )

    room_101 = next(s for s in summaries if s["room_number"] == "101")
    assert room_101["has_current_bed"] is True
    assert room_101["total_beds"] == 2
    assert room_101["occupied_target_beds"] == 1  # Bed B is target
    assert room_101["vacant_beds"] == 0

    room_102 = next(s for s in summaries if s["room_number"] == "102")
    assert room_102["has_current_bed"] is False
    assert room_102["vacant_beds"] == 1
    assert room_102["occupied_target_beds"] == 0


@pytest.mark.django_db
def test_room_bed_details_api_distinguishes_states_and_does_not_leak_student_info(
    client,
    student_user,
    other_student_user,
    multi_room_hostel_setup,
    warden_user,
):
    assign_student_to_bed(
        student=student_user,
        bed=multi_room_hostel_setup["bed_a"],
        created_by=warden_user,
    )
    assign_student_to_bed(
        student=other_student_user,
        bed=multi_room_hostel_setup["bed_b"],
        created_by=warden_user,
    )

    client.force_login(student_user)
    response = client.get(
        reverse("exchange:api_room_beds", kwargs={"room_id": multi_room_hostel_setup["room"].id}),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["room"]["room_number"] == "101"
    beds = data["beds"]
    assert len(beds) == 2

    # Bed A is current student's bed: non-selectable, tagged 'current'
    bed_a_data = next(b for b in beds if b["bed_label"] == "Bed A")
    assert bed_a_data["is_current"] is True
    assert bed_a_data["selectable"] is False
    assert bed_a_data["status_code"] == "current"

    # Bed B is occupied by another student: selectable exchange target
    bed_b_data = next(b for b in beds if b["bed_label"] == "Bed B")
    assert bed_b_data["is_current"] is False
    assert bed_b_data["is_occupied"] is True
    assert bed_b_data["selectable"] is True
    assert bed_b_data["status_code"] == "occupied_target"

    # Verify no student identity information is leaked
    content_str = response.content.decode()
    assert other_student_user.full_name not in content_str
    assert other_student_user.email not in content_str
    assert "REG-002" not in content_str


@pytest.mark.django_db
def test_selected_bed_is_authoritative_and_resolves_destination_relationships(
    client,
    student_user,
    multi_room_hostel_setup,
    warden_user,
):
    assign_student_to_bed(
        student=student_user,
        bed=multi_room_hostel_setup["bed_a"],
        created_by=warden_user,
    )
    client.force_login(student_user)

    # Submit with only preferred_bed and reason, omitting redundant dropdowns
    target_bed = multi_room_hostel_setup["bed_c"]
    response = client.post(
        reverse("exchange:request_create"),
        {
            "preferred_bed": str(target_bed.id),
            "reason": "Requesting swap to room 102 single bed for academic study focus.",
        },
    )

    assert response.status_code == 302
    req = ExchangeRequest.objects.get(student=student_user)
    assert req.preferred_bed_id == target_bed.id
    # Backend resolved destination hierarchy automatically from DB
    assert req.preferred_hostel_id == target_bed.room.floor.block.hostel_id
    assert req.preferred_block_id == target_bed.room.floor.block_id
    assert req.preferred_floor_id == target_bed.room.floor_id
    assert req.preferred_room_type == target_bed.room.room_type


@pytest.mark.django_db
def test_reason_minimum_length_enforced(
    client,
    student_user,
    multi_room_hostel_setup,
    warden_user,
):
    assign_student_to_bed(
        student=student_user,
        bed=multi_room_hostel_setup["bed_a"],
        created_by=warden_user,
    )
    client.force_login(student_user)

    target_bed = multi_room_hostel_setup["bed_c"]
    response = client.post(
        reverse("exchange:request_create"),
        {
            "preferred_bed": str(target_bed.id),
            "reason": "Too short",  # 9 characters (< 10)
        },
    )

    assert response.status_code == 200
    assert "Please provide at least 10 characters" in response.content.decode()
    assert not ExchangeRequest.objects.filter(student=student_user).exists()


@pytest.mark.django_db
def test_preferences_mode_submission(
    client,
    student_user,
    multi_room_hostel_setup,
    warden_user,
):
    assign_student_to_bed(
        student=student_user,
        bed=multi_room_hostel_setup["bed_a"],
        created_by=warden_user,
    )
    client.force_login(student_user)

    # Submit in preferences mode without preferred_bed
    response = client.post(
        reverse("exchange:request_create"),
        {
            "preferred_bed": "",
            "preferred_hostel": str(multi_room_hostel_setup["hostel"].id),
            "preferred_block": str(multi_room_hostel_setup["block"].id),
            "preferred_room_type": Room.RoomType.SINGLE,
            "reason": "I am seeking any available single room within North Wing.",
        },
    )

    assert response.status_code == 302
    req = ExchangeRequest.objects.get(student=student_user)
    assert req.preferred_bed is None
    assert req.preferred_hostel_id == multi_room_hostel_setup["hostel"].id
    assert req.preferred_room_type == Room.RoomType.SINGLE
