from django.db.models import Count, Q, QuerySet

from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
)

from .models import ExchangeRequest


def get_student_exchange_requests(student) -> QuerySet:
    """Return exchange requests owned by the student, newest first."""
    return (
        ExchangeRequest.objects.filter(student=student)
        .select_related(
            "current_occupancy__bed__room__floor__block__hostel",
            "preferred_bed__room__floor__block__hostel",
            "preferred_hostel",
            "preferred_block",
            "preferred_floor",
        )
        .order_by("-submitted_at", "-created_at")
    )


def get_student_exchange_request(student, request_id):
    """Return one exchange request only when it belongs to the student."""
    return get_student_exchange_requests(student).filter(id=request_id).first()


def get_student_target_beds(student) -> QuerySet:
    """Return in-service beds a student may select, including occupied beds."""
    eligible_categories = [
        Hostel.ResidentCategory.MALE,
        Hostel.ResidentCategory.FEMALE,
    ]
    if student.residential_eligibility in eligible_categories:
        eligible_categories = [student.residential_eligibility]
    else:
        eligible_categories = []
    return (
        Bed.objects.filter(
            is_active=True,
            operational_status=OperationalStatus.IN_SERVICE,
            room__is_active=True,
            room__operational_status="in_service",
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel__institution=student.institution,
            room__floor__block__hostel__status=Hostel.Status.ACTIVE,
            room__floor__block__hostel__resident_category__in=eligible_categories,
        )
        .select_related("room__floor__block__hostel")
        .order_by(
            "room__floor__block__hostel__name",
            "room__floor__block__name",
            "room__floor__floor_number",
            "room__room_number",
            "bed_label",
        )
    )


def get_student_target_rooms(student, *, floor_id=None, room_id=None) -> QuerySet:
    """Return active rooms for the visual target browser."""
    queryset = (
        Room.objects.filter(
            is_active=True,
            floor__is_active=True,
            floor__block__is_active=True,
            floor__block__hostel__institution=student.institution,
            floor__block__hostel__status=Hostel.Status.ACTIVE,
            floor__block__hostel__resident_category=student.residential_eligibility,
        )
        .select_related("floor__block__hostel")
        .prefetch_related("beds", "beds__occupancies")
        .order_by("room_number")
    )
    if floor_id:
        queryset = queryset.filter(floor_id=floor_id)
    if room_id:
        queryset = queryset.filter(id=room_id)
    return queryset


def get_eligible_hostels_for_exchange(student) -> QuerySet:
    """Return hostels matching student's residential eligibility and institution."""
    return Hostel.objects.filter(
        institution=student.institution,
        status=Hostel.Status.ACTIVE,
        resident_category=student.residential_eligibility,
    ).order_by("name")


def get_eligible_blocks_for_hostel(student, hostel_id) -> QuerySet:
    """Return active blocks for an eligible hostel."""
    return Block.objects.filter(
        hostel_id=hostel_id,
        hostel__institution=student.institution,
        hostel__status=Hostel.Status.ACTIVE,
        hostel__resident_category=student.residential_eligibility,
        is_active=True,
    ).order_by("name")


def get_eligible_floors_for_block(student, block_id) -> QuerySet:
    """Return active floors for an eligible block."""
    return Floor.objects.filter(
        block_id=block_id,
        block__hostel__institution=student.institution,
        block__hostel__status=Hostel.Status.ACTIVE,
        block__hostel__resident_category=student.residential_eligibility,
        block__is_active=True,
        is_active=True,
    ).order_by("floor_number")


def get_floor_room_summaries_for_exchange(student, floor_id: int | str) -> list[dict]:
    """
    Return compact room summaries for a selected floor.
    Calculates bed counts without materializing all beds or resident records in memory.
    """
    current_occupancy = (
        StudentOccupancy.objects.filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .values_list("bed_id", flat=True)
        .first()
    )

    rooms = (
        Room.objects.filter(
            floor_id=floor_id,
            floor__is_active=True,
            floor__block__is_active=True,
            floor__block__hostel__institution=student.institution,
            floor__block__hostel__status=Hostel.Status.ACTIVE,
            floor__block__hostel__resident_category=student.residential_eligibility,
        )
        .select_related("floor__block__hostel")
        .prefetch_related(
            "beds",
            "beds__occupancies",
        )
        .order_by("room_number")
    )

    summaries = []
    for room in rooms:
        beds = room.beds.all()
        total_beds = len(beds)
        vacant_beds = 0
        occupied_target_beds = 0
        unavailable_beds = 0
        has_current_bed = False

        room_ok = (
            room.is_active
            and room.operational_status == OperationalStatus.IN_SERVICE
        )

        for bed in beds:
            is_active_occupant = any(
                occ.status == StudentOccupancy.OccupancyStatus.ACTIVE
                for occ in bed.occupancies.all()
            )
            is_current = (current_occupancy is not None and bed.id == current_occupancy)
            if is_current:
                has_current_bed = True

            bed_ok = (
                room_ok
                and bed.is_active
                and bed.operational_status == OperationalStatus.IN_SERVICE
            )

            if not bed_ok:
                unavailable_beds += 1
            elif is_current:
                pass  # Current student's own bed (neither vacant nor swap target)
            elif is_active_occupant:
                occupied_target_beds += 1
            else:
                vacant_beds += 1

        capacity = Room.ROOM_TYPE_CAPACITY.get(room.room_type, total_beds)

        summaries.append({
            "id": room.id,
            "room_number": room.room_number,
            "room_type": room.room_type,
            "room_type_display": room.get_room_type_display(),
            "capacity": capacity,
            "occupancy_category": room.occupancy_category or "Standard",
            "operational_status": room.operational_status,
            "is_active": room.is_active,
            "total_beds": total_beds,
            "vacant_beds": vacant_beds,
            "occupied_target_beds": occupied_target_beds,
            "unavailable_beds": unavailable_beds,
            "has_current_bed": has_current_bed,
        })

    return summaries


def get_room_bed_details_for_exchange(student, room_id: int | str) -> dict | None:
    """
    Return detailed bed data for one specific room, strictly verifying institution and eligibility.
    Distinguishes Vacant, Occupied (swap target), Current Bed, and Unavailable states without
    leaking any student identities or sensitive info.
    """
    room = (
        Room.objects.filter(
            id=room_id,
            floor__is_active=True,
            floor__block__is_active=True,
            floor__block__hostel__institution=student.institution,
            floor__block__hostel__status=Hostel.Status.ACTIVE,
            floor__block__hostel__resident_category=student.residential_eligibility,
        )
        .select_related("floor__block__hostel")
        .prefetch_related("beds", "beds__occupancies")
        .first()
    )
    if not room:
        return None

    current_occupancy_bed_id = (
        StudentOccupancy.objects.filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .values_list("bed_id", flat=True)
        .first()
    )

    room_in_service = (
        room.is_active
        and room.operational_status == OperationalStatus.IN_SERVICE
    )

    bed_list = []
    for bed in room.beds.all().order_by("bed_label"):
        is_current = (current_occupancy_bed_id is not None and bed.id == current_occupancy_bed_id)
        has_active_occupancy = any(
            occ.status == StudentOccupancy.OccupancyStatus.ACTIVE
            for occ in bed.occupancies.all()
        )
        bed_in_service = (
            bed.is_active
            and bed.operational_status == OperationalStatus.IN_SERVICE
            and room_in_service
        )

        if is_current:
            status_code = "current"
            status_label = "Your current bed"
            selectable = False
        elif not bed_in_service:
            status_code = "unavailable"
            status_label = "Unavailable"
            selectable = False
        elif has_active_occupancy:
            status_code = "occupied_target"
            status_label = "Occupied — exchange target"
            selectable = True
        else:
            status_code = "vacant"
            status_label = "Vacant"
            selectable = True

        bed_list.append({
            "id": bed.id,
            "bed_label": bed.bed_label,
            "status_code": status_code,
            "status_label": status_label,
            "selectable": selectable,
            "is_current": is_current,
            "is_vacant": status_code == "vacant",
            "is_occupied": status_code == "occupied_target",
        })

    return {
        "room": {
            "id": room.id,
            "room_number": room.room_number,
            "room_type": room.room_type,
            "room_type_display": room.get_room_type_display(),
            "capacity": Room.ROOM_TYPE_CAPACITY.get(room.room_type, len(bed_list)),
            "occupancy_category": room.occupancy_category or "Standard",
            "hostel_name": room.floor.block.hostel.name,
            "hostel_id": room.floor.block.hostel.id,
            "block_name": room.floor.block.name,
            "block_id": room.floor.block.id,
            "floor_label": room.floor.floor_label,
            "floor_id": room.floor.id,
        },
        "beds": bed_list,
    }

