import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from apps.hostels.models import StudentOccupancy, OperationalStatus
from apps.hostels.services import (
    assign_student_to_bed,
    release_student_occupancy,
    update_bed_status,
)


@pytest.mark.django_db
def test_student_allocation_succeeds(student_user, hostel_setup, admin_user):
    bed = hostel_setup["bed_a"]
    occupancy = assign_student_to_bed(
        student=student_user,
        bed=bed,
        created_by=admin_user,
    )
    assert occupancy.id is not None
    assert occupancy.status == StudentOccupancy.OccupancyStatus.ACTIVE
    assert occupancy.student == student_user
    assert occupancy.bed == bed
    assert bed.is_occupied


@pytest.mark.django_db
def test_student_cannot_hold_two_active_occupancies(student_user, hostel_setup, admin_user):
    bed_a = hostel_setup["bed_a"]
    bed_b = hostel_setup["bed_b"]

    assign_student_to_bed(student=student_user, bed=bed_a, created_by=admin_user)

    with pytest.raises(ValidationError):
        assign_student_to_bed(student=student_user, bed=bed_b, created_by=admin_user)


@pytest.mark.django_db
def test_bed_cannot_have_two_active_occupants(student_user, other_student_user, hostel_setup, admin_user):
    bed = hostel_setup["bed_a"]

    assign_student_to_bed(student=student_user, bed=bed, created_by=admin_user)

    with pytest.raises(ValidationError):
        assign_student_to_bed(student=other_student_user, bed=bed, created_by=admin_user)


@pytest.mark.django_db
def test_maintenance_bed_cannot_receive_allocation(student_user, hostel_setup, admin_user):
    bed = hostel_setup["bed_a"]
    update_bed_status(bed=bed, new_status=OperationalStatus.MAINTENANCE, acting_user=admin_user)

    with pytest.raises(ValidationError):
        assign_student_to_bed(student=student_user, bed=bed, created_by=admin_user)


@pytest.mark.django_db
def test_cross_institution_allocation_rejected(student_user, other_institution, admin_user, roles):
    from apps.hostels.models import Hostel, Block, Floor, Room, Bed
    h = Hostel.objects.create(institution=other_institution, name="External Hall", code="EXT-01")
    b = Block.objects.create(hostel=h, name="Block A", code="BA")
    f = Floor.objects.create(block=b, floor_number=1, floor_label="1st Floor")
    r = Room.objects.create(floor=f, room_number="101", room_type="single")
    bed = Bed.objects.create(room=r, bed_label="Bed A")

    with pytest.raises(ValidationError):
        assign_student_to_bed(student=student_user, bed=bed, created_by=admin_user)


@pytest.mark.django_db
def test_releasing_allocation_preserves_history(student_user, hostel_setup, admin_user):
    bed = hostel_setup["bed_a"]
    occupancy = assign_student_to_bed(student=student_user, bed=bed, created_by=admin_user)

    released = release_student_occupancy(
        occupancy_id=occupancy.id,
        release_reason="Semester graduation checkout",
        acting_user=admin_user,
    )
    assert released.status == StudentOccupancy.OccupancyStatus.RELEASED
    assert released.end_date is not None
    assert released.release_reason == "Semester graduation checkout"

    # Bed is now vacant again and can be allocated to another student
    assert not bed.is_occupied
