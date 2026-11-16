import pytest
from django.urls import reverse
from django.core.exceptions import PermissionDenied, ValidationError
from django.test.utils import setup_test_environment
from apps.hostels.models import (
    Hostel,
    Block,
    Floor,
    Room,
    Bed,
    WardenAssignment,
    StudentOccupancy,
    OperationalStatus,
)
from apps.hostels.selectors import (
    get_warden_assigned_hostel_ids,
    get_warden_assigned_hostels,
    get_hostel_queryset_with_stats,
    get_room_inventory_queryset,
    get_vacant_beds,
)
from apps.hostels.services import assign_student_to_bed

try:
    setup_test_environment()
except Exception:
    pass


@pytest.fixture
def two_hostels_setup(institution, warden_user, student_user, other_student_user):
    # Hostel A - Assigned to warden
    hostel_a = Hostel.objects.create(
        institution=institution,
        name="Hostel Alpha",
        code="HA-01",
        resident_category=Hostel.ResidentCategory.MALE,
        status=Hostel.Status.ACTIVE,
    )
    block_a = Block.objects.create(hostel=hostel_a, name="Block A", code="BA")
    floor_a = Floor.objects.create(block=block_a, floor_number=1, floor_label="Floor 1")
    room_a = Room.objects.create(floor=floor_a, room_number="101", room_type=Room.RoomType.DOUBLE)
    bed_a1 = Bed.objects.create(room=room_a, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)
    bed_a2 = Bed.objects.create(room=room_a, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    # Assign warden to Hostel A
    WardenAssignment.objects.create(
        warden=warden_user,
        hostel=hostel_a,
        is_primary=True,
    )

    # Occupancy in Hostel A
    occ_a = StudentOccupancy.objects.create(
        institution=institution,
        student=student_user,
        bed=bed_a1,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        created_by=warden_user,
    )

    # Hostel B - NOT assigned to warden
    hostel_b = Hostel.objects.create(
        institution=institution,
        name="Hostel Beta",
        code="HB-02",
        resident_category=Hostel.ResidentCategory.MALE,
        status=Hostel.Status.ACTIVE,
    )
    block_b = Block.objects.create(hostel=hostel_b, name="Block B", code="BB")
    floor_b = Floor.objects.create(block=block_b, floor_number=1, floor_label="Floor 1")
    room_b = Room.objects.create(floor=floor_b, room_number="201", room_type=Room.RoomType.DOUBLE)
    bed_b1 = Bed.objects.create(room=room_b, bed_label="A", operational_status=OperationalStatus.IN_SERVICE)
    bed_b2 = Bed.objects.create(room=room_b, bed_label="B", operational_status=OperationalStatus.IN_SERVICE)

    # Occupancy in Hostel B
    occ_b = StudentOccupancy.objects.create(
        institution=institution,
        student=other_student_user,
        bed=bed_b1,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
    )

    return {
        "hostel_a": hostel_a,
        "room_a": room_a,
        "bed_a1": bed_a1,
        "bed_a2": bed_a2,
        "occ_a": occ_a,
        "hostel_b": hostel_b,
        "room_b": room_b,
        "bed_b1": bed_b1,
        "bed_b2": bed_b2,
        "occ_b": occ_b,
    }


@pytest.mark.django_db
def test_warden_assigned_single_hostel_scoping(client, warden_user, two_hostels_setup):
    data = two_hostels_setup
    hostel_a = data["hostel_a"]
    hostel_b = data["hostel_b"]
    room_a = data["room_a"]
    room_b = data["room_b"]
    bed_a2 = data["bed_a2"]
    bed_b2 = data["bed_b2"]
    occ_a = data["occ_a"]
    occ_b = data["occ_b"]

    # 1. Selectors check
    assigned_ids = get_warden_assigned_hostel_ids(warden_user)
    assert assigned_ids == [hostel_a.id]

    hostels_qs = get_hostel_queryset_with_stats(institution=warden_user.institution, user=warden_user)
    assert list(hostels_qs.values_list("id", flat=True)) == [hostel_a.id]

    rooms_qs = get_room_inventory_queryset(institution=warden_user.institution, user=warden_user)
    assert list(rooms_qs.values_list("id", flat=True)) == [room_a.id]

    vacant_beds = get_vacant_beds(institution=warden_user.institution, user=warden_user)
    assert list(vacant_beds.values_list("id", flat=True)) == [bed_a2.id]

    # 2. Dashboard page check
    client.force_login(warden_user)
    resp = client.get(reverse("accounts:dashboard_warden"))
    assert resp.status_code == 200
    assert hostel_a.name in resp.content.decode()
    assert hostel_b.name not in resp.content.decode()

    # 3. Hostel Explorer list check
    resp = client.get(reverse("hostels:hostel_list"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert hostel_a.name in content
    assert hostel_b.name not in content
    assert resp.context["total_count"] == 1

    # 4. Room inventory page check
    resp = client.get(reverse("hostels:room_inventory"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert room_a.room_number in content
    assert room_b.room_number not in content
    filter_hostel_ids = [h.id for h in resp.context["filter_hostels"]]
    assert filter_hostel_ids == [hostel_a.id]

    # 5. Bed list check
    resp = client.get(reverse("hostels:bed_list"))
    assert resp.status_code == 200
    bed_page_ids = [b.id for b in resp.context["page_obj"]]
    assert data["bed_a1"].id in bed_page_ids
    assert data["bed_a2"].id in bed_page_ids
    assert data["bed_b1"].id not in bed_page_ids
    assert data["bed_b2"].id not in bed_page_ids

    # 6. Occupancy roster check
    resp = client.get(reverse("hostels:occupancy_list"))
    assert resp.status_code == 200
    occ_page_ids = [o.id for o in resp.context["page_obj"]]
    assert occ_a.id in occ_page_ids
    assert occ_b.id not in occ_page_ids


@pytest.mark.django_db
def test_warden_assigned_multiple_hostels(client, warden_user, two_hostels_setup):
    data = two_hostels_setup
    hostel_a = data["hostel_a"]
    hostel_b = data["hostel_b"]

    # Assign warden to Hostel B as well
    WardenAssignment.objects.create(warden=warden_user, hostel=hostel_b, is_primary=False)

    # Create unassigned Hostel C
    hostel_c = Hostel.objects.create(
        institution=warden_user.institution,
        name="Hostel Gamma",
        code="HG-03",
        resident_category=Hostel.ResidentCategory.MALE,
    )

    client.force_login(warden_user)
    resp = client.get(reverse("hostels:hostel_list"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert hostel_a.name in content
    assert hostel_b.name in content
    assert hostel_c.name not in content
    assert resp.context["total_count"] == 2


@pytest.mark.django_db
def test_warden_with_zero_assignments(client, warden_user, two_hostels_setup):
    # Remove all assignments
    WardenAssignment.objects.filter(warden=warden_user).delete()

    client.force_login(warden_user)
    unassigned_notice = "No hostel has been assigned to your account. Contact the hostel administration."

    # Dashboard
    resp = client.get(reverse("accounts:dashboard_warden"))
    assert resp.status_code == 200
    assert unassigned_notice in resp.content.decode()
    assert resp.context["total_rooms"] == 0
    assert resp.context["total_beds"] == 0

    # Hostel list
    resp = client.get(reverse("hostels:hostel_list"))
    assert resp.status_code == 200
    assert unassigned_notice in resp.content.decode()
    assert resp.context["total_count"] == 0

    # Room inventory
    resp = client.get(reverse("hostels:room_inventory"))
    assert resp.status_code == 200
    assert unassigned_notice in resp.content.decode()

    # Bed list
    resp = client.get(reverse("hostels:bed_list"))
    assert resp.status_code == 200
    assert unassigned_notice in resp.content.decode()

    # Occupancy list
    resp = client.get(reverse("hostels:occupancy_list"))
    assert resp.status_code == 200
    assert unassigned_notice in resp.content.decode()

    # Allocation page
    resp = client.get(reverse("hostels:occupancy_assign"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_warden_direct_access_to_unassigned_records_denied(client, warden_user, two_hostels_setup):
    data = two_hostels_setup
    hostel_b = data["hostel_b"]
    room_b = data["room_b"]
    bed_b2 = data["bed_b2"]
    occ_b = data["occ_b"]

    client.force_login(warden_user)

    # 1. Hostel detail for unassigned hostel -> 404
    resp = client.get(reverse("hostels:hostel_detail", kwargs={"pk": hostel_b.id}))
    assert resp.status_code == 404

    # 2. Room detail for unassigned room -> 403
    resp = client.get(reverse("hostels:room_detail", kwargs={"pk": room_b.id}))
    assert resp.status_code == 403

    # 3. Occupancy detail for unassigned occupancy -> 403
    resp = client.get(reverse("hostels:occupancy_detail", kwargs={"pk": occ_b.id}))
    assert resp.status_code == 403

    # 4. Occupancy release for unassigned occupancy -> 403
    resp = client.get(reverse("hostels:occupancy_release", kwargs={"pk": occ_b.id}))
    assert resp.status_code == 403

    # 5. Bed status update for unassigned bed -> 403
    resp = client.post(reverse("hostels:bed_status_update", kwargs={"pk": bed_b2.id}), {"operational_status": OperationalStatus.MAINTENANCE})
    assert resp.status_code == 403


@pytest.mark.django_db
def test_warden_staff_superuser_flag_does_not_bypass_scoping(client, warden_user, two_hostels_setup):
    data = two_hostels_setup
    hostel_b = data["hostel_b"]
    room_b = data["room_b"]
    occ_b = data["occ_b"]

    # Give warden user staff and superuser privileges
    warden_user.is_staff = True
    warden_user.is_superuser = True
    warden_user.save()

    client.force_login(warden_user)

    # Selectors still scoped
    hostels_qs = get_hostel_queryset_with_stats(institution=warden_user.institution, user=warden_user)
    assert list(hostels_qs.values_list("id", flat=True)) == [data["hostel_a"].id]

    # Unassigned hostel detail still 404
    resp = client.get(reverse("hostels:hostel_detail", kwargs={"pk": hostel_b.id}))
    assert resp.status_code == 404

    # Unassigned room detail still 403
    resp = client.get(reverse("hostels:room_detail", kwargs={"pk": room_b.id}))
    assert resp.status_code == 403

    # Unassigned occupancy detail still 403
    resp = client.get(reverse("hostels:occupancy_detail", kwargs={"pk": occ_b.id}))
    assert resp.status_code == 403
