import pytest
from django.urls import reverse
from apps.hostels.models import Hostel, WardenAssignment


@pytest.mark.django_db
def test_student_cannot_access_hostel_creation(client, student_user):
    client.force_login(student_user)
    response = client.get(reverse("hostels:hostel_create"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_student_cannot_access_room_creation(client, student_user):
    client.force_login(student_user)
    response = client.get(reverse("hostels:room_create"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_student_cannot_access_occupancy_roster(client, student_user):
    client.force_login(student_user)
    response = client.get(reverse("hostels:occupancy_list"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_warden_cannot_access_unassigned_hostel_room(client, warden_user, other_institution, roles):
    # Create hostel where warden is not assigned
    unassigned_hostel = Hostel.objects.create(
        institution=warden_user.institution,
        name="Unassigned Hall",
        code="UH-01",
    )
    from apps.hostels.models import Block, Floor, Room, Bed
    block = Block.objects.create(hostel=unassigned_hostel, name="Block X", code="BX")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="1st Floor")
    room = Room.objects.create(floor=floor, room_number="999", room_type="single")

    client.force_login(warden_user)
    response = client.get(reverse("hostels:room_detail", kwargs={"pk": room.id}))
    assert response.status_code == 403


@pytest.mark.django_db
def test_cross_institution_access_rejected(client, admin_user, other_institution):
    other_hostel = Hostel.objects.create(
        institution=other_institution,
        name="Foreign Hall",
        code="FH-01",
    )
    from apps.hostels.models import Block, Floor, Room
    b = Block.objects.create(hostel=other_hostel, name="Block F", code="BF")
    fl = Floor.objects.create(block=b, floor_number=1, floor_label="1st Floor")
    rm = Room.objects.create(floor=fl, room_number="505", room_type="single")

    client.force_login(admin_user)
    response = client.get(reverse("hostels:room_detail", kwargs={"pk": rm.id}))
    assert response.status_code == 403
