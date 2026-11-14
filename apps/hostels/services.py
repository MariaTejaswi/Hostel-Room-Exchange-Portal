import logging
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.core.models import AuditLog, Institution
from apps.accounts.models import User
from apps.accounts.permissions import (
    can_manage_infrastructure,
    can_edit_hostel,
    can_manage_rooms,
    can_update_bed_status as can_update_bed_status_perm,
)
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
    resident_category: str = Hostel.ResidentCategory.MALE,
    address: str = "",
    description: str = "",
    status: str = Hostel.Status.ACTIVE,
    acting_user: User | None = None,
    ip_address: str | None = None,
) -> Hostel:
    """Create a new hostel under an institution."""
    if acting_user and not can_manage_infrastructure(acting_user):
        raise ValidationError("Only Dean of Student Welfare and Hostel Administrators are authorized to create hostels.")
    
    if resident_category == "coed":
        raise ValidationError("Co-ed hostels are not supported. RoomLoop supports exactly two categories: 'male' (Boys' Hostel) and 'female' (Girls' Hostel).")
    if resident_category not in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
        raise ValidationError(
            f"Invalid resident category '{resident_category}'. RoomLoop supports exactly two categories: "
            f"'{Hostel.ResidentCategory.MALE}' (Boys' Hostel) and '{Hostel.ResidentCategory.FEMALE}' (Girls' Hostel)."
        )

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
    """Update hostel properties with safety validation against removing occupied capacity."""
    if acting_user and not can_edit_hostel(acting_user, hostel):
        raise ValidationError("You do not have permission to edit this hostel.")

    if resident_category == "coed":
        raise ValidationError("Co-ed hostels are not supported. RoomLoop supports exactly two categories: 'male' (Boys' Hostel) and 'female' (Girls' Hostel).")
    if resident_category not in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE] and resident_category != hostel.resident_category:
        raise ValidationError(
            f"Invalid resident category '{resident_category}'. RoomLoop supports exactly two categories: "
            f"'{Hostel.ResidentCategory.MALE}' (Boys' Hostel) and '{Hostel.ResidentCategory.FEMALE}' (Girls' Hostel)."
        )

    if status != Hostel.Status.ACTIVE:
        active_occupancies_count = StudentOccupancy.objects.filter(
            bed__room__floor__block__hostel=hostel,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).count()
        if active_occupancies_count > 0:
            raise ValidationError(
                f"Cannot deactivate hostel '{hostel.name}' because {active_occupancies_count} student(s) currently hold active bed allocations. Please reassign or release occupants before deactivating."
            )

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
    if acting_user and not can_manage_rooms(acting_user, floor.block.hostel):
        raise ValidationError("You do not have permission to create rooms in this hostel.")

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
    if acting_user and not can_update_bed_status_perm(acting_user, bed):
        raise ValidationError("You do not have permission to update bed operational status.")

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

    # Authority checks on acting staff
    if created_by:
        if created_by.is_dean_role:
            raise ValidationError("The Dean of Student Welfare is not permitted to perform direct student bed allocations.")
        if created_by.is_sysadmin_role:
            raise ValidationError("System Administrators are not permitted to perform operational bed allocations.")
        if created_by.is_student_role:
            raise ValidationError("Students cannot allocate accommodation.")
        if created_by.is_hostel_admin_role:
            raise ValidationError(
                "Hostel Administrators cannot perform routine bed allocations. Emergency relocations require a dedicated audited emergency workflow."
            )
        if not (created_by.is_warden_role or created_by.is_chief_warden_role):
            raise ValidationError("You do not have operational authority to allocate student accommodation.")
        if created_by.is_warden_role and not created_by.is_chief_warden_role:
            if not WardenAssignment.objects.filter(warden=created_by, hostel=locked_bed.room.floor.block.hostel).exists():
                raise ValidationError("Wardens can only allocate beds within their assigned hostels.")

    institution = locked_bed.room.floor.block.hostel.institution

    if locked_student.institution_id != institution.id:
        raise ValidationError("Student and Hostel belong to different institutions.")

    # 5. Authoritative Residential Eligibility Verification
    hostel = locked_bed.room.floor.block.hostel
    student_eligibility = getattr(locked_student, "residential_eligibility", None)

    if hostel.resident_category in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
        if not student_eligibility or student_eligibility == "unverified":
            raise ValidationError(
                f"Cannot allocate student {locked_student.full_name}: Residential eligibility is unverified. "
                "Institutional verification by the Welfare Office or Registrar is required before allocation."
            )
        if student_eligibility != hostel.resident_category:
            raise ValidationError(
                f"Cannot allocate student {locked_student.full_name}: Student's verified eligibility is "
                f"'{locked_student.get_residential_eligibility_display()}', which does not match hostel category "
                f"'{hostel.get_resident_category_display()}'."
            )

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

    if acting_user:
        if acting_user.is_dean_role:
            raise ValidationError("The Dean of Student Welfare is not permitted to perform direct occupancy releases.")
        if acting_user.is_sysadmin_role:
            raise ValidationError("System Administrators are not permitted to release student accommodation.")
        if acting_user.is_student_role:
            raise ValidationError("Students cannot release accommodation.")
        if acting_user.is_hostel_admin_role:
            raise ValidationError(
                "Hostel Administrators cannot perform routine occupancy releases. Emergency relocations require a dedicated audited emergency workflow."
            )
        if not (acting_user.is_warden_role or acting_user.is_chief_warden_role):
            raise ValidationError("You do not have operational authority to release student accommodation.")

    occupancy = (
        StudentOccupancy.objects.select_for_update()
        .select_related("student", "bed__room__floor__block__hostel", "institution")
        .get(id=occupancy_id)
    )

    if acting_user and acting_user.is_warden_role and not acting_user.is_chief_warden_role:
        hostel = occupancy.bed.room.floor.block.hostel
        if not WardenAssignment.objects.filter(warden=acting_user, hostel=hostel).exists():
            raise ValidationError("Wardens can only release allocations within their assigned hostels.")

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


@transaction.atomic
def save_custom_hostel_configuration(
    *,
    institution: Institution,
    hostel: Hostel | None = None,
    configuration_data: dict | None = None,
    acting_user: User | None = None,
    data: dict | None = None,
    user: User | None = None,
    hostel_id: int | None = None,
    change_reason: str = "",
    ip_address: str | None = None,
) -> Hostel:
    """
    Atomic Custom Hostel Builder persistence service.
    
    Supports:
    - Creating new or editing existing hostel infrastructure.
    - Independent blocks, floors, rooms, and variable beds per room.
    - Arbitrary room capacities (single, double, triple, quad, 5-bed, 6-bed, dormitory, etc.).
    - Strict server-side authorization: Dean of Student Welfare and Hostel Administrator only.
    - Institution-scoped validation and child-ID anti-tampering verification.
    - Row-level locking in consistent top-down hierarchical order.
    - Stale edit detection using microsecond-precision timestamps.
    - Safe editing: blocks category changes or deactivation of any infrastructure containing active occupants.
    - Non-destructive soft deactivation (is_active=False) preserving historical allocations and audit references.
    """
    if configuration_data is None:
        configuration_data = data or {}
    if acting_user is None:
        acting_user = user
    if hostel is None and hostel_id is not None:
        try:
            hostel = Hostel.objects.get(id=hostel_id, institution=institution)
        except Hostel.DoesNotExist:
            raise ValidationError(f"Hostel with ID {hostel_id} does not exist in this institution.")

    # 1. Authorization & Scoping
    if not acting_user or not can_manage_infrastructure(acting_user):
        raise ValidationError(
            "Access Denied: Only Dean of Student Welfare and Hostel Administrators are authorized to configure hostel infrastructure."
        )
    if acting_user.institution and institution.id != acting_user.institution.id:
        raise ValidationError("Unauthorized: You cannot manage hostel infrastructure outside your institution.")

    # 2. Extract & Validate Hostel Metadata
    name = str(configuration_data.get("name", "")).strip()
    code = str(configuration_data.get("code", "")).strip().upper()
    resident_category = configuration_data.get("resident_category", Hostel.ResidentCategory.MALE)
    address = str(configuration_data.get("address", "")).strip()
    description = str(configuration_data.get("description", "")).strip()
    status = configuration_data.get("status", Hostel.Status.ACTIVE)

    if len(name) < 2:
        raise ValidationError("Hostel name must be at least 2 characters long.")
    if len(code) < 2:
        raise ValidationError("Hostel code must be at least 2 characters long.")
    if resident_category == "coed":
        raise ValidationError("Co-ed hostels are not supported. RoomLoop supports exactly two categories: 'male' (Boys' Hostel) and 'female' (Girls' Hostel).")
    if resident_category not in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
        # Allow existing legacy category on edit if unchanged
        if not (hostel and resident_category == hostel.resident_category):
            raise ValidationError(
                f"Invalid resident category: '{resident_category}'. RoomLoop supports exactly two categories: "
                f"'{Hostel.ResidentCategory.MALE}' (Boys' Hostel) and '{Hostel.ResidentCategory.FEMALE}' (Girls' Hostel)."
            )
    if status not in Hostel.Status.values:
        raise ValidationError(f"Invalid hostel status: '{status}'.")

    # 3. Lock or Create Hostel
    is_new = hostel is None
    if not is_new:
        # Hierarchical lock on hostel row
        locked_hostel = Hostel.objects.select_for_update().get(id=hostel.id, institution=institution)

        # Stale Edit Concurrency Check
        initial_updated_at = configuration_data.get("initial_updated_at")
        if initial_updated_at and locked_hostel.updated_at:
            if locked_hostel.updated_at.isoformat() != initial_updated_at:
                raise ValidationError(
                    "Stale edit detected: This hostel was modified by another administrator since you began editing. "
                    "Please reload the latest configuration before applying changes."
                )

        # Safety: Category change blocked if active occupancies exist
        if resident_category != locked_hostel.resident_category:
            active_count = StudentOccupancy.objects.filter(
                bed__room__floor__block__hostel=locked_hostel,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).count()
            if active_count > 0:
                raise ValidationError(
                    f"Cannot change hostel resident category to '{resident_category}' while {active_count} active "
                    f"student allocation(s) exist in this hostel. All residents must be relocated first."
                )

        # Safety: Status change to inactive blocked if active occupancies exist
        if status != Hostel.Status.ACTIVE:
            active_count = StudentOccupancy.objects.filter(
                bed__room__floor__block__hostel=locked_hostel,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).count()
            if active_count > 0:
                raise ValidationError(
                    f"Cannot deactivate hostel '{locked_hostel.name}' because {active_count} student(s) currently hold active bed allocations."
                )

        # Uniqueness of code within institution
        if Hostel.objects.filter(institution=institution, code=code).exclude(id=locked_hostel.id).exists():
            raise ValidationError(f"A hostel with code '{code}' already exists in this institution.")

        locked_hostel.name = name
        locked_hostel.code = code
        locked_hostel.resident_category = resident_category
        locked_hostel.address = address
        locked_hostel.description = description
        locked_hostel.status = status
        locked_hostel.full_clean()
        locked_hostel.save()
    else:
        if Hostel.objects.filter(institution=institution, code=code).exists():
            raise ValidationError(f"A hostel with code '{code}' already exists in this institution.")

        locked_hostel = Hostel(
            institution=institution,
            name=name,
            code=code,
            resident_category=resident_category,
            address=address,
            description=description,
            status=status,
        )
        locked_hostel.full_clean()
        locked_hostel.save()

    # 4. Lock Existing Hierarchy (in stable top-down order)
    existing_blocks = {}
    existing_floors = {}
    existing_rooms = {}
    existing_beds = {}

    if not is_new:
        existing_blocks = {
            b.id: b
            for b in Block.objects.select_for_update().filter(hostel=locked_hostel).order_by("id")
        }
        existing_floors = {
            f.id: f
            for f in Floor.objects.select_for_update().filter(block__hostel=locked_hostel).order_by("id")
        }
        existing_rooms = {
            r.id: r
            for r in Room.objects.select_for_update().filter(floor__block__hostel=locked_hostel).order_by("id")
        }
        existing_beds = {
            bd.id: bd
            for bd in Bed.objects.select_for_update().filter(room__floor__block__hostel=locked_hostel).order_by("id")
        }

    # 5. Payload Validation & Limits
    blocks_payload = configuration_data.get("blocks", [])
    if not blocks_payload:
        raise ValidationError("At least one block is required to configure a hostel.")
    if len(blocks_payload) > 10:
        raise ValidationError("A hostel cannot have more than 10 blocks.")

    submitted_block_ids = set()
    submitted_floor_ids = set()
    submitted_room_ids = set()
    submitted_bed_ids = set()

    seen_block_codes = set()

    # Pass 1: Comprehensive Validation across the entire change set
    for b_idx, block_data in enumerate(blocks_payload):
        b_name = str(block_data.get("name", f"Block {b_idx + 1}")).strip()
        b_code = str(block_data.get("code", f"BLK-{b_idx + 1}")).strip().upper()
        b_id = block_data.get("id")

        if not b_name:
            raise ValidationError(f"Block name cannot be empty (Block #{b_idx + 1}).")
        if not b_code:
            raise ValidationError(f"Block code cannot be empty (Block '{b_name}').")
        if b_code in seen_block_codes:
            raise ValidationError(f"Duplicate block code '{b_code}' in hostel configuration.")
        seen_block_codes.add(b_code)

        if b_id:
            try:
                b_id_int = int(b_id)
                if b_id_int not in existing_blocks:
                    raise ValidationError(f"Invalid block ID {b_id}: does not belong to hostel '{locked_hostel.name}'.")
                submitted_block_ids.add(b_id_int)
            except (ValueError, TypeError):
                pass

        floors_payload = block_data.get("floors", [])
        if len(floors_payload) > 20:
            raise ValidationError(f"Block '{b_name}' exceeds maximum limit of 20 floors.")

        seen_floor_numbers = set()
        for f_idx, floor_data in enumerate(floors_payload):
            f_num = floor_data.get("floor_number", f_idx)
            try:
                f_num = int(f_num)
            except (ValueError, TypeError):
                raise ValidationError(f"Floor number must be an integer (Floor #{f_idx + 1} in Block '{b_name}').")

            if f_num in seen_floor_numbers:
                raise ValidationError(f"Duplicate floor number {f_num} in Block '{b_name}'.")
            seen_floor_numbers.add(f_num)

            f_label = str(floor_data.get("floor_label", f"Floor {f_num}")).strip()
            if not f_label:
                raise ValidationError(f"Floor label cannot be empty (Floor {f_num} in Block '{b_name}').")

            f_id = floor_data.get("id")
            if f_id:
                try:
                    f_id_int = int(f_id)
                    if f_id_int not in existing_floors:
                        raise ValidationError(f"Invalid floor ID {f_id}: does not belong to this hostel.")
                    if b_id and existing_floors[f_id_int].block_id != int(b_id):
                        raise ValidationError(f"Floor ID {f_id} belongs to a different block than submitted.")
                    submitted_floor_ids.add(f_id_int)
                except (ValueError, TypeError):
                    pass

            rooms_payload = floor_data.get("rooms", [])
            if len(rooms_payload) > 100:
                raise ValidationError(f"Floor '{f_label}' exceeds maximum limit of 100 rooms.")

            seen_room_numbers = set()
            for r_idx, room_data in enumerate(rooms_payload):
                r_num = str(room_data.get("room_number", f"{f_num * 100 + r_idx + 1}")).strip()
                if not r_num:
                    raise ValidationError(f"Room number cannot be empty on Floor '{f_label}'.")
                if r_num in seen_room_numbers:
                    raise ValidationError(f"Duplicate room number '{r_num}' on Floor '{f_label}'.")
                seen_room_numbers.add(r_num)

                r_id = room_data.get("id")
                if r_id:
                    try:
                        r_id_int = int(r_id)
                        if r_id_int not in existing_rooms:
                            raise ValidationError(f"Invalid room ID {r_id}: does not belong to this hostel.")
                        if f_id and existing_rooms[r_id_int].floor_id != int(f_id):
                            raise ValidationError(f"Room ID {r_id} belongs to a different floor than submitted.")
                        submitted_room_ids.add(r_id_int)
                    except (ValueError, TypeError):
                        pass

                beds_payload = room_data.get("beds", [])
                bed_count = len(beds_payload)
                if bed_count < 1:
                    raise ValidationError(f"Room '{r_num}' must have at least 1 bed.")
                if bed_count > 24:
                    raise ValidationError(f"Room '{r_num}' exceeds maximum capacity limit of 24 beds.")

                seen_bed_labels = set()
                for b_item_idx, bed_data in enumerate(beds_payload):
                    b_label = str(bed_data.get("bed_label", f"Bed {b_item_idx + 1}")).strip()
                    if not b_label:
                        raise ValidationError(f"Bed label cannot be empty in Room '{r_num}'.")
                    if b_label in seen_bed_labels:
                        raise ValidationError(f"Duplicate bed label '{b_label}' in Room '{r_num}'.")
                    seen_bed_labels.add(b_label)

                    bd_id = bed_data.get("id")
                    if bd_id:
                        try:
                            bd_id_int = int(bd_id)
                            if bd_id_int not in existing_beds:
                                raise ValidationError(f"Invalid bed ID {bd_id}: does not belong to this hostel.")
                            if r_id and existing_beds[bd_id_int].room_id != int(r_id):
                                raise ValidationError(f"Bed ID {bd_id} belongs to a different room than submitted.")
                            submitted_bed_ids.add(bd_id_int)
                        except (ValueError, TypeError):
                            pass

    # 6. Safety Checks against Deactivating Occupied Infrastructure
    # A. Check beds proposed for removal / omission
    for existing_bed_id, existing_bed in existing_beds.items():
        if existing_bed_id not in submitted_bed_ids:
            # Bed is omitted or removed in the new configuration
            active_occ = StudentOccupancy.objects.filter(
                bed=existing_bed,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).select_related("student").first()
            if active_occ:
                raise ValidationError(
                    f"Safety Violation: Cannot remove or deactivate Bed '{existing_bed.bed_label}' in Room "
                    f"'{existing_bed.room.room_number}' because student {active_occ.student.full_name} "
                    f"({active_occ.student.registration_number or active_occ.student.email}) currently holds an active allocation. "
                    f"The occupant must be reallocated before reducing capacity."
                )
            # Safe to soft-deactivate
            existing_bed.is_active = False
            existing_bed.save(update_fields=["is_active", "updated_at"])

    # B. Check rooms proposed for removal
    for existing_room_id, existing_room in existing_rooms.items():
        if existing_room_id not in submitted_room_ids:
            active_occ = StudentOccupancy.objects.filter(
                bed__room=existing_room,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).select_related("student", "bed").first()
            if active_occ:
                raise ValidationError(
                    f"Safety Violation: Cannot remove or deactivate Room '{existing_room.room_number}' because Bed "
                    f"'{active_occ.bed.bed_label}' is actively occupied by {active_occ.student.full_name}."
                )
            existing_room.is_active = False
            existing_room.save(update_fields=["is_active", "updated_at"])

    # C. Check floors proposed for removal
    for existing_floor_id, existing_floor in existing_floors.items():
        if existing_floor_id not in submitted_floor_ids:
            active_occ = StudentOccupancy.objects.filter(
                bed__room__floor=existing_floor,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).first()
            if active_occ:
                raise ValidationError(
                    f"Safety Violation: Cannot remove or deactivate Floor '{existing_floor.floor_label}' because it contains active student occupants."
                )
            existing_floor.is_active = False
            existing_floor.save(update_fields=["is_active", "updated_at"])

    # D. Check blocks proposed for removal
    for existing_block_id, existing_block in existing_blocks.items():
        if existing_block_id not in submitted_block_ids:
            active_occ = StudentOccupancy.objects.filter(
                bed__room__floor__block=existing_block,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).first()
            if active_occ:
                raise ValidationError(
                    f"Safety Violation: Cannot remove or deactivate Block '{existing_block.name}' because it contains active student occupants."
                )
            existing_block.is_active = False
            existing_block.save(update_fields=["is_active", "updated_at"])

    # 7. Upsert Active Infrastructure (Blocks -> Floors -> Rooms -> Beds)
    created_rooms_count = 0
    created_beds_count = 0

    for block_data in blocks_payload:
        b_name = str(block_data.get("name", "Main Block")).strip()
        b_code = str(block_data.get("code", "BLK-M")).strip().upper()
        b_desc = str(block_data.get("description", "")).strip()
        b_id = block_data.get("id")

        if b_id and int(b_id) in existing_blocks:
            block = existing_blocks[int(b_id)]
            block.name = b_name
            block.code = b_code
            block.description = b_desc
            block.is_active = True
            block.full_clean()
            block.save()
        else:
            block = Block(
                hostel=locked_hostel,
                name=b_name,
                code=b_code,
                description=b_desc,
                is_active=True,
            )
            block.full_clean()
            block.save()

        for floor_data in block_data.get("floors", []):
            f_num = int(floor_data.get("floor_number", 0))
            f_label = str(floor_data.get("floor_label", f"Floor {f_num}")).strip()
            f_id = floor_data.get("id")

            if f_id and int(f_id) in existing_floors:
                floor = existing_floors[int(f_id)]
                floor.block = block
                floor.floor_number = f_num
                floor.floor_label = f_label
                floor.is_active = True
                floor.full_clean()
                floor.save()
            else:
                floor = Floor(
                    block=block,
                    floor_number=f_num,
                    floor_label=f_label,
                    is_active=True,
                )
                floor.full_clean()
                floor.save()

            for room_data in floor_data.get("rooms", []):
                r_num = str(room_data.get("room_number", "")).strip()
                beds_payload = room_data.get("beds", [])
                bed_count = len(beds_payload)

                # Determine room type choice based on bed count
                if bed_count == 1:
                    r_type = Room.RoomType.SINGLE
                elif bed_count == 2:
                    r_type = Room.RoomType.DOUBLE
                elif bed_count == 3:
                    r_type = Room.RoomType.TRIPLE
                elif bed_count == 4:
                    r_type = Room.RoomType.FOUR
                elif bed_count == 5:
                    r_type = Room.RoomType.FIVE
                elif bed_count == 6:
                    r_type = Room.RoomType.SIX
                else:
                    r_type = Room.RoomType.DORMITORY

                # Override with explicit room_type if provided and valid
                explicit_type = room_data.get("room_type")
                if explicit_type in Room.RoomType.values:
                    r_type = explicit_type

                r_cat = str(room_data.get("occupancy_category", "Standard")).strip()
                r_status = room_data.get("operational_status", OperationalStatus.IN_SERVICE)
                r_id = room_data.get("id")

                if r_id and int(r_id) in existing_rooms:
                    room = existing_rooms[int(r_id)]
                    room.floor = floor
                    room.room_number = r_num
                    room.room_type = r_type
                    room.occupancy_category = r_cat
                    room.operational_status = r_status
                    room.is_active = True
                    room.full_clean()
                    room.save()
                else:
                    room = Room(
                        floor=floor,
                        room_number=r_num,
                        room_type=r_type,
                        occupancy_category=r_cat,
                        operational_status=r_status,
                        is_active=True,
                    )
                    room.full_clean()
                    room.save()
                    created_rooms_count += 1

                for bed_data in beds_payload:
                    bd_label = str(bed_data.get("bed_label", "")).strip()
                    bd_status = bed_data.get("operational_status", OperationalStatus.IN_SERVICE)
                    bd_id = bed_data.get("id")

                    if bd_id and int(bd_id) in existing_beds:
                        bed = existing_beds[int(bd_id)]
                        bed.room = room
                        bed.bed_label = bd_label
                        bed.operational_status = bd_status
                        bed.is_active = True
                        bed.full_clean()
                        bed.save()
                    else:
                        bed = Bed(
                            room=room,
                            bed_label=bd_label,
                            operational_status=bd_status,
                            is_active=True,
                        )
                        bed.full_clean()
                        bed.save()
                        created_beds_count += 1

    # 8. Audit History Recording
    summary_action = "HOSTEL_CREATED" if is_new else "HOSTEL_CONFIG_UPDATED"
    audit_summary = (
        f"{'Created' if is_new else 'Updated configuration for'} hostel {locked_hostel.name} ({locked_hostel.code}). "
        f"Configured blocks={len(blocks_payload)}, rooms_created={created_rooms_count}, beds_created={created_beds_count}."
    )
    if change_reason:
        audit_summary += f" Reason: {change_reason.strip()}"

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=institution,
        action=summary_action,
        target_type="Hostel",
        target_id=str(locked_hostel.id),
        change_summary=audit_summary,
        ip_address=ip_address,
    )

    return locked_hostel

