import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from apps.hostels.models import Hostel, Room, Bed, OperationalStatus
from apps.hostels.services import create_hostel, create_room, update_bed_status


@pytest.mark.django_db
def test_hostel_creation_succeeds(institution, admin_user):
    hostel = create_hostel(
        institution=institution,
        name="East Residence",
        code="ER-01",
        resident_category=Hostel.ResidentCategory.COED,
        acting_user=admin_user,
    )
    assert hostel.id is not None
    assert hostel.code == "ER-01"


@pytest.mark.django_db
def test_duplicate_hostel_code_rejected(institution, admin_user):
    create_hostel(
        institution=institution,
        name="Hostel One",
        code="H-01",
        acting_user=admin_user,
    )
    with pytest.raises(ValidationError):
        create_hostel(
            institution=institution,
            name="Hostel Two",
            code="H-01",
            acting_user=admin_user,
        )


@pytest.mark.django_db
def test_duplicate_bed_label_rejected(hostel_setup):
    room = hostel_setup["room"]
    with pytest.raises(IntegrityError):
        Bed.objects.create(room=room, bed_label="Bed A")


@pytest.mark.django_db
def test_room_creation_generates_correct_beds(hostel_setup, admin_user):
    floor = hostel_setup["floor"]
    room = create_room(
        floor=floor,
        room_number="102",
        room_type=Room.RoomType.TRIPLE,
        create_beds=True,
        acting_user=admin_user,
    )
    assert room.beds.count() == 3
    assert set(room.beds.values_list("bed_label", flat=True)) == {"Bed A", "Bed B", "Bed C"}
