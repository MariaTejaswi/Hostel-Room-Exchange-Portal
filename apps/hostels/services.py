import logging
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.core.models import AuditLog, Institution
from apps.accounts.models import User
from .models import (
    Hostel,
    Block,
    Floor,
    Room,
    Bed,
    WardenAssignment,
    StudentOccupancy,
    OperationalStatus,
)

logger = logging.getLogger(__name__)


@transaction.atomic
def create_hostel(
    *,
    institution: Institution,
    name: str,
    code: str,
    resident_category: str = Hostel.ResidentCategory.COED,
    address: str = "",
    description: str = "",
    status: str = Hostel.Status.ACTIVE,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> Hostel:
    """Create a new hostel under an institution."""
    code = code.strip().upper()
    if Hostel.objects.filter(institution=institution, code=code).exists():
        raise ValidationError(f"A hostel with code '{code}' already exists in this institution.")

    hostel = Hostel(
        institution=institution,
        name=name.strip(),
        code=code,
        resident_category=resident_category,
        address=address.strip(),
        description=description.strip(),
        status=status,
    )
    hostel.full_clean()
    hostel.save()

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=institution,
        action="HOSTEL_CREATED",
        target_type="Hostel",
        target_id=str(hostel.id),
        change_summary=f"Created hostel {hostel.name} ({hostel.code})",
        ip_address=ip_address,
    )
    return hostel


@transaction.atomic
def update_hostel(
    *,
    hostel: Hostel,
    name: str,
    resident_category: str,
    address: str,
    description: str,
    status: str,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> Hostel:
    """Update hostel properties."""
    hostel.name = name.strip()
    hostel.resident_category = resident_category
    hostel.address = address.strip()
    hostel.description = description.strip()
    hostel.status = status
    hostel.full_clean()
    hostel.save()

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=hostel.institution,
        action="HOSTEL_UPDATED",
        target_type="Hostel",
        target_id=str(hostel.id),
        change_summary=f"Updated hostel {hostel.name}",
        ip_address=ip_address,
    )
    return hostel


@transaction.atomic
def create_room(
    *,
    floor: Floor,
    room_number: str,
    room_type: str,
    occupancy_category: str = "Standard",
    accessibility_attributes: str = "",
    description: str = "",
    operational_status: str = OperationalStatus.IN_SERVICE,
    create_beds: bool = True,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> Room:
    """Create a room and optionally generate its default beds based on room_type capacity."""
    room_number = room_number.strip()
    if Room.objects.filter(floor=floor, room_number=room_number).exists():
        raise ValidationError(
            f"Room '{room_number}' already exists on floor '{floor.floor_label}'."
        )

    room = Room(
        floor=floor,
        room_number=room_number,
        room_type=room_type,
        occupancy_category=occupancy_category.strip(),
        accessibility_attributes=accessibility_attributes.strip(),
        description=description.strip(),
        operational_status=operational_status,
    )
    room.full_clean()
    room.save()

    if create_beds:
        capacity = room.target_capacity
        bed_labels = ["Bed A", "Bed B", "Bed C", "Bed D"][:capacity]
        for label in bed_labels:
            Bed.objects.create(
                room=room,
                bed_label=label,
                operational_status=OperationalStatus.IN_SERVICE,
            )

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=floor.block.hostel.institution,
        action="ROOM_CREATED",
        target_type="Room",
        target_id=str(room.id),
        change_summary=f"Created Room {room.room_number} with capacity {room.target_capacity}",
        ip_address=ip_address,
    )
    return room


@transaction.atomic
def update_bed_status(
    *,
    bed: Bed,
    new_status: str,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> Bed:
    """Update bed operational status safely."""
    if new_status not in OperationalStatus.values:
        raise ValidationError(f"Invalid operational status: {new_status}")

    # Prevent moving an occupied bed to maintenance without first reallocating or releasing occupant
    if new_status != OperationalStatus.IN_SERVICE and bed.is_occupied:
        raise ValidationError(
            "Cannot set bed under maintenance or out of service while an active student occupancy exists. Please release or reassign the occupant first."
        )

    old_status = bed.operational_status
    bed.operational_status = new_status
    bed.save(update_fields=["operational_status", "updated_at"])

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=bed.room.floor.block.hostel.institution,
        action="BED_STATUS_CHANGED",
        target_type="Bed",
        target_id=str(bed.id),
        change_summary=f"Changed bed {bed} status from {old_status} to {new_status}",
        ip_address=ip_address,
    )
    return bed


@transaction.atomic
def assign_student_to_bed(
    *,
    student: User,
    bed: Bed,
    start_date=None,
    source: str = StudentOccupancy.AllocationSource.ADMINISTRATIVE,
    created_by: User | None = None,
    ip_address: str | None = None,
) -> StudentOccupancy:
    """
    Allocate a student to a vacant bed with row-level locking.
    Enforces all domain business logic and constraints:
    1. Student and Bed must belong to the same institution.
    2. Bed must be IN_SERVICE.
    3. Bed must not have an active occupant.
    4. Student must not currently hold an active allocation.
    """
    # Use select_for_update on both bed and student to prevent concurrent double-booking
    locked_bed = Bed.objects.select_for_update().select_related("room__floor__block__hostel__institution").get(id=bed.id)
    locked_student = User.objects.select_for_update().get(id=student.id)

    institution = locked_bed.room.floor.block.hostel.institution

    if locked_student.institution_id != institution.id:
        raise ValidationError("Student and Hostel belong to different institutions.")

    if locked_bed.operational_status != OperationalStatus.IN_SERVICE:
        raise ValidationError(
            f"Bed {locked_bed.bed_label} cannot receive allocations because its status is '{locked_bed.get_operational_status_display()}'."
        )

    # Check for active occupancy on this bed
    if StudentOccupancy.objects.filter(bed=locked_bed, status=StudentOccupancy.OccupancyStatus.ACTIVE).exists():
        raise ValidationError(f"Bed {locked_bed.bed_label} in Room {locked_bed.room.room_number} is already occupied.")

    # Check for active occupancy on this student
    if StudentOccupancy.objects.filter(student=locked_student, status=StudentOccupancy.OccupancyStatus.ACTIVE).exists():
        raise ValidationError(
            f"Student {locked_student.full_name} ({locked_student.email}) already has an active room allocation."
        )

    start_date = start_date or timezone.now().date()

    occupancy = StudentOccupancy(
        institution=institution,
        student=locked_student,
        bed=locked_bed,
        start_date=start_date,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        source=source,
        created_by=created_by,
    )
    occupancy.full_clean()
    occupancy.save()

    AuditLog.objects.create(
        acting_user=created_by or locked_student,
        institution=institution,
        action="OCCUPANCY_ASSIGNED",
        target_type="StudentOccupancy",
        target_id=str(occupancy.id),
        change_summary=f"Assigned student {locked_student.full_name} ({locked_student.registration_number}) to {locked_bed.room.room_number} - {locked_bed.bed_label}",
        ip_address=ip_address,
    )
    return occupancy


@transaction.atomic
def release_student_occupancy(
    *,
    occupancy_id: int,
    release_reason: str,
    end_date=None,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> StudentOccupancy:
    """
    Release an active student occupancy safely, preserving historical allocation record.
    """
    if not release_reason or len(release_reason.strip()) < 5:
        raise ValidationError("A detailed release reason (minimum 5 characters) is required.")

    occupancy = (
        StudentOccupancy.objects.select_for_update()
        .select_related("student", "bed__room", "institution")
        .get(id=occupancy_id)
    )

    if occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE:
        raise ValidationError(
            f"This occupancy record is already '{occupancy.get_status_display()}' and cannot be released again."
        )

    end_date = end_date or timezone.now().date()
    if end_date < occupancy.start_date:
        raise ValidationError("Release end date cannot be earlier than allocation start date.")

    occupancy.status = StudentOccupancy.OccupancyStatus.RELEASED
    occupancy.end_date = end_date
    occupancy.release_reason = release_reason.strip()
    occupancy.full_clean()
    occupancy.save(update_fields=["status", "end_date", "release_reason", "updated_at"])

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=occupancy.institution,
        action="OCCUPANCY_RELEASED",
        target_type="StudentOccupancy",
        target_id=str(occupancy.id),
        change_summary=f"Released occupancy for {occupancy.student.full_name} from {occupancy.bed}. Reason: {occupancy.release_reason}",
        ip_address=ip_address,
    )
    return occupancy


@transaction.atomic
def assign_warden_to_hostel(
    *,
    warden: User,
    hostel: Hostel,
    is_primary: bool = False,
    acting_user: User | None = None,
) -> WardenAssignment:
    """Assign a warden to a hostel."""
    assignment, created = WardenAssignment.objects.get_or_create(
        warden=warden,
        hostel=hostel,
        defaults={"is_primary": is_primary},
    )
    if not created and assignment.is_primary != is_primary:
        assignment.is_primary = is_primary
        assignment.save(update_fields=["is_primary"])
    return assignment
