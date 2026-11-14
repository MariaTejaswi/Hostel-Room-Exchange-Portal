import logging
from django.db.models import Subquery, OuterRef, Count, Q, F, Value, IntegerField
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
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


def get_hostel_queryset_with_stats(institution=None, user=None):
    """
    Fetch hostels with real aggregate database statistics using correlated subqueries
    to prevent Cartesian join inflation:
    - total_rooms: active rooms in active floors and blocks
    - total_beds: total active bed capacity in active rooms/floors/blocks
    - total_inventory_beds: all physical bed records (including historical/deactivated)
    - occupied_beds: active beds currently holding an active student occupancy
    - maintenance_beds: active beds currently in MAINTENANCE status
    - available_beds / available_operational_beds: allocatable in-service beds with no active occupancy
    """
    qs = Hostel.objects.all().select_related("institution")

    if institution:
        qs = qs.filter(institution=institution)

    # Scoping for wardens without administrative authority
    if user and user.is_warden_role and not (user.is_chief_warden_role or user.is_hostel_admin_role):
        assigned_hostel_ids = WardenAssignment.objects.filter(warden=user).values_list("hostel_id", flat=True)
        qs = qs.filter(id__in=assigned_hostel_ids)

    active_occupant_bed_ids = StudentOccupancy.objects.filter(
        status=StudentOccupancy.OccupancyStatus.ACTIVE
    ).values("bed_id")

    # Correlated subqueries against OuterRef("pk") of Hostel
    sub_total_floors = Subquery(
        Floor.objects.filter(
            is_active=True,
            block__is_active=True,
            block__hostel=OuterRef("pk"),
        )
        .values("block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    sub_total_rooms = Subquery(
        Room.objects.filter(
            is_active=True,
            floor__is_active=True,
            floor__block__is_active=True,
            floor__block__hostel=OuterRef("pk"),
        )
        .values("floor__block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    sub_active_capacity = Subquery(
        Bed.objects.filter(
            is_active=True,
            room__is_active=True,
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel=OuterRef("pk"),
        )
        .values("room__floor__block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    sub_total_inventory = Subquery(
        Bed.objects.filter(
            room__floor__block__hostel=OuterRef("pk"),
        )
        .values("room__floor__block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    sub_occupied_beds = Subquery(
        Bed.objects.filter(
            is_active=True,
            room__is_active=True,
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel=OuterRef("pk"),
            id__in=active_occupant_bed_ids,
        )
        .values("room__floor__block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    sub_maintenance_beds = Subquery(
        Bed.objects.filter(
            is_active=True,
            room__is_active=True,
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel=OuterRef("pk"),
            operational_status=OperationalStatus.MAINTENANCE,
        )
        .values("room__floor__block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    sub_available_beds = Subquery(
        Bed.objects.filter(
            is_active=True,
            operational_status=OperationalStatus.IN_SERVICE,
            room__is_active=True,
            room__operational_status=OperationalStatus.IN_SERVICE,
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel=OuterRef("pk"),
            room__floor__block__hostel__status=Hostel.Status.ACTIVE,
        )
        .exclude(id__in=active_occupant_bed_ids)
        .values("room__floor__block__hostel")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    qs = qs.annotate(
        total_floors=Coalesce(sub_total_floors, Value(0)),
        total_rooms=Coalesce(sub_total_rooms, Value(0)),
        total_beds=Coalesce(sub_active_capacity, Value(0)),
        total_inventory_beds=Coalesce(sub_total_inventory, Value(0)),
        occupied_beds=Coalesce(sub_occupied_beds, Value(0)),
        maintenance_beds=Coalesce(sub_maintenance_beds, Value(0)),
        available_beds=Coalesce(sub_available_beds, Value(0)),
        available_operational_beds=Coalesce(sub_available_beds, Value(0)),
    )
    return qs


def get_room_inventory_queryset(
    institution=None,
    hostel_id=None,
    block_id=None,
    floor_id=None,
    room_type=None,
    operational_status=None,
    search_query=None,
    availability=None,
    user=None,
):
    """
    Filterable, searchable room inventory queryset with bed and occupancy annotations.
    Uses correlated subqueries on room primary keys to prevent join inflation.
    """
    qs = (
        Room.objects.filter(is_active=True)
        .select_related("floor__block__hostel__institution")
        .prefetch_related("beds", "beds__occupancies", "beds__occupancies__student")
    )

    if institution:
        qs = qs.filter(floor__block__hostel__institution=institution)

    if user and user.is_warden_role and not (user.is_chief_warden_role or user.is_hostel_admin_role):
        assigned_hostels = WardenAssignment.objects.filter(warden=user).values_list("hostel_id", flat=True)
        qs = qs.filter(floor__block__hostel_id__in=assigned_hostels)

    if hostel_id:
        qs = qs.filter(floor__block__hostel_id=hostel_id)
    if block_id:
        qs = qs.filter(floor__block_id=block_id)
    if floor_id:
        qs = qs.filter(floor_id=floor_id)
    if room_type:
        qs = qs.filter(room_type=room_type)
    if operational_status:
        qs = qs.filter(operational_status=operational_status)
    if search_query:
        qs = qs.filter(
            Q(room_number__icontains=search_query)
            | Q(floor__block__name__icontains=search_query)
            | Q(floor__block__hostel__name__icontains=search_query)
        )

    active_occupant_bed_ids = StudentOccupancy.objects.filter(
        status=StudentOccupancy.OccupancyStatus.ACTIVE
    ).values("bed_id")

    sub_room_beds = Subquery(
        Bed.objects.filter(is_active=True, room=OuterRef("pk"))
        .values("room")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )
    sub_room_occupied = Subquery(
        Bed.objects.filter(is_active=True, room=OuterRef("pk"), id__in=active_occupant_bed_ids)
        .values("room")
        .annotate(c=Count("id"))
        .values("c"),
        output_field=IntegerField(),
    )

    qs = qs.annotate(
        total_beds_count=Coalesce(sub_room_beds, Value(0)),
        occupied_beds_count=Coalesce(sub_room_occupied, Value(0)),
    )

    if availability == "vacant":
        qs = qs.filter(occupied_beds_count__lt=F("total_beds_count"))
    elif availability == "full":
        qs = qs.filter(occupied_beds_count__gte=F("total_beds_count"), total_beds_count__gt=0)

    return qs


def get_student_active_occupancy(student: User):
    """Return the currently active occupancy record for a student, if any."""
    return (
        StudentOccupancy.objects.filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .select_related(
            "bed__room__floor__block__hostel__institution",
            "bed__room",
            "institution",
        )
        .first()
    )


def get_student_verified_eligibility(user: User | None) -> str | None:
    """
    Return the authoritative verified residential eligibility for a student.
    Returns 'male', 'female', or None if unverified.
    Strictly enforces that eligibility is NOT inferred from current occupancy,
    student names, or arbitrary guesses.
    """
    if not user or not user.is_authenticated:
        return None
    eligibility = getattr(user, "residential_eligibility", None)
    if eligibility in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
        return eligibility
    return None


def get_eligible_unallocated_students(institution, required_category: str | None = None):
    """
    Return active students within the institution who currently do NOT have an active bed assignment,
    optionally filtered by their verified residential eligibility.
    """
    active_occupant_ids = StudentOccupancy.objects.filter(
        institution=institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
    ).values_list("student_id", flat=True)

    qs = (
        User.objects.filter(
            institution=institution,
            is_active=True,
            groups__name="Student",
        )
        .exclude(id__in=active_occupant_ids)
        .order_by("full_name")
    )
    if required_category:
        qs = qs.filter(residential_eligibility=required_category)
    return qs


def get_vacant_beds(institution, hostel_id=None):
    """Return in-service beds that have no active occupant in active hierarchy."""
    occupied_bed_ids = StudentOccupancy.objects.filter(
        institution=institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
    ).values_list("bed_id", flat=True)

    qs = (
        Bed.objects.filter(
            is_active=True,
            operational_status=OperationalStatus.IN_SERVICE,
            room__is_active=True,
            room__operational_status=OperationalStatus.IN_SERVICE,
            room__floor__is_active=True,
            room__floor__block__is_active=True,
            room__floor__block__hostel__status=Hostel.Status.ACTIVE,
            room__floor__block__hostel__institution=institution,
        )
        .exclude(id__in=occupied_bed_ids)
        .select_related("room__floor__block__hostel")
        .order_by("room__floor__block__hostel__name", "room__room_number", "bed_label")
    )

    if hostel_id:
        qs = qs.filter(room__floor__block__hostel_id=hostel_id)
    return qs


# Custom Hostel Builder Selectors
def get_hostel_builder_initial_data(hostel: Hostel | None = None) -> dict:
    """
    Serialize hostel hierarchy for the interactive Custom Hostel Builder.
    If hostel is None, returns an empty template with a default block.
    """
    if hostel is None:
        return {
            "id": None,
            "name": "",
            "code": "",
            "resident_category": Hostel.ResidentCategory.MALE,
            "address": "",
            "description": "",
            "status": Hostel.Status.ACTIVE,
            "initial_updated_at": "",
            "change_reason": "",
            "blocks": [
                {
                    "id": None,
                    "name": "Main Block",
                    "code": "BLK-M",
                    "description": "Primary Residential Wing",
                    "is_active": True,
                    "floors": [
                        {
                            "id": None,
                            "floor_number": 1,
                            "floor_label": "1st Floor",
                            "is_active": True,
                            "rooms": [],
                        }
                    ],
                }
            ],
        }

    # Fetch existing hostel with full active hierarchy
    blocks_data = []
    blocks = (
        Block.objects.filter(hostel=hostel, is_active=True)
        .prefetch_related(
            "floors",
            "floors__rooms",
            "floors__rooms__beds",
            "floors__rooms__beds__occupancies",
            "floors__rooms__beds__occupancies__student",
        )
        .order_by("name")
    )

    for block in blocks:
        floors_data = []
        for floor in block.floors.filter(is_active=True).order_by("floor_number"):
            rooms_data = []
            for room in floor.rooms.filter(is_active=True).order_by("room_number"):
                beds_data = []
                for bed in room.beds.filter(is_active=True).order_by("bed_label"):
                    active_occ = bed.occupancies.filter(status=StudentOccupancy.OccupancyStatus.ACTIVE).first()
                    beds_data.append({
                        "id": bed.id,
                        "bed_label": bed.bed_label,
                        "operational_status": bed.operational_status,
                        "is_active": bed.is_active,
                        "is_occupied": active_occ is not None,
                        "occupant_name": active_occ.student.full_name if active_occ else None,
                        "occupant_reg_no": active_occ.student.registration_number if active_occ else None,
                    })

                rooms_data.append({
                    "id": room.id,
                    "room_number": room.room_number,
                    "room_type": room.room_type,
                    "occupancy_category": room.occupancy_category,
                    "operational_status": room.operational_status,
                    "is_active": room.is_active,
                    "beds": beds_data,
                })

            floors_data.append({
                "id": floor.id,
                "floor_number": floor.floor_number,
                "floor_label": floor.floor_label,
                "is_active": floor.is_active,
                "rooms": rooms_data,
            })

        blocks_data.append({
            "id": block.id,
            "name": block.name,
            "code": block.code,
            "description": block.description,
            "is_active": block.is_active,
            "floors": floors_data,
        })

    return {
        "id": hostel.id,
        "name": hostel.name,
        "code": hostel.code,
        "resident_category": hostel.resident_category,
        "address": hostel.address,
        "description": hostel.description,
        "status": hostel.status,
        "initial_updated_at": hostel.updated_at.isoformat() if hostel.updated_at else "",
        "change_reason": "",
        "blocks": blocks_data,
    }


def calculate_configuration_metrics(config_data: dict) -> dict:
    """
    Calculate live metrics and review breakdown from a submitted or drafted builder payload.
    Ensures preview counts match persisted results exactly.
    """
    blocks = config_data.get("blocks", [])
    total_blocks = len([b for b in blocks if b.get("is_active", True)])
    total_floors = 0
    total_rooms = 0
    total_beds = 0
    available_beds = 0
    maintenance_beds = 0
    floor_breakdown = []

    for block in blocks:
        if not block.get("is_active", True):
            continue
        for floor in block.get("floors", []):
            if not floor.get("is_active", True):
                continue
            total_floors += 1
            floor_rooms = [r for r in floor.get("rooms", []) if r.get("is_active", True)]
            floor_room_count = len(floor_rooms)
            floor_bed_count = 0
            for r in floor_rooms:
                for bd in r.get("beds", []):
                    if not bd.get("is_active", True):
                        continue
                    floor_bed_count += 1
                    status = bd.get("operational_status", OperationalStatus.IN_SERVICE)
                    if status == OperationalStatus.MAINTENANCE:
                        maintenance_beds += 1
                    elif status == OperationalStatus.IN_SERVICE:
                        available_beds += 1

            total_rooms += floor_room_count
            total_beds += floor_bed_count

            floor_breakdown.append({
                "block_name": block.get("name", "Block"),
                "floor_label": floor.get("floor_label", f"Floor {floor.get('floor_number')}"),
                "rooms_count": floor_room_count,
                "beds_count": floor_bed_count,
                "room_numbers": [r.get("room_number") for r in floor_rooms],
            })

    return {
        "total_blocks": total_blocks,
        "total_floors": total_floors,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "total_capacity": total_beds,
        "available_beds": available_beds,
        "maintenance_beds": maintenance_beds,
        "floor_breakdown": floor_breakdown,
    }


# Role-Specific Dashboard Context Selectors
def get_student_dashboard_context(user: User) -> dict:
    occupancy = get_student_active_occupancy(user)
    history = (
        StudentOccupancy.objects.filter(student=user)
        .exclude(status=StudentOccupancy.OccupancyStatus.ACTIVE)
        .select_related("bed__room__floor__block__hostel")
        .order_by("-start_date")[:5]
    )
    return {
        "current_occupancy": occupancy,
        "occupancy_history": history,
        "student": user,
    }


def get_warden_dashboard_context(user: User) -> dict:
    hostels_qs = get_hostel_queryset_with_stats(institution=user.institution, user=user)
    hostels = list(hostels_qs)

    total_rooms = sum(h.total_rooms for h in hostels)
    total_beds = sum(h.total_beds for h in hostels)
    occupied_beds = sum(h.occupied_beds for h in hostels)
    available_beds = sum(h.available_beds for h in hostels)
    maintenance_beds = sum(h.maintenance_beds for h in hostels)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0

    assigned_hostel_ids = [h.id for h in hostels]
    recent_occupancies = (
        StudentOccupancy.objects.filter(
            bed__room__floor__block__hostel_id__in=assigned_hostel_ids,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .select_related("student", "bed__room__floor__block__hostel")
        .order_by("-created_at")[:10]
    )

    return {
        "hostels": hostels,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "occupied_beds": occupied_beds,
        "available_beds": available_beds,
        "available_operational_beds": available_beds,
        "maintenance_beds": maintenance_beds,
        "occupancy_pct": occupancy_pct,
        "recent_occupancies": recent_occupancies,
    }


def get_chief_warden_dashboard_context(user: User) -> dict:
    hostels_qs = get_hostel_queryset_with_stats(institution=user.institution)
    hostels = list(hostels_qs)

    total_hostels = len(hostels)
    total_rooms = sum(h.total_rooms for h in hostels)
    total_beds = sum(h.total_beds for h in hostels)
    occupied_beds = sum(h.occupied_beds for h in hostels)
    available_beds = sum(h.available_beds for h in hostels)
    maintenance_beds = sum(h.maintenance_beds for h in hostels)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0

    return {
        "hostels": hostels,
        "total_hostels": total_hostels,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "occupied_beds": occupied_beds,
        "available_beds": available_beds,
        "available_operational_beds": available_beds,
        "maintenance_beds": maintenance_beds,
        "occupancy_pct": occupancy_pct,
    }


def get_admin_dashboard_context(user: User) -> dict:
    institution = user.institution
    hostels_qs = get_hostel_queryset_with_stats(institution=institution)
    hostels = list(hostels_qs)

    total_hostels = len(hostels)
    total_blocks = Block.objects.filter(hostel__institution=institution, is_active=True).count() if institution else Block.objects.filter(is_active=True).count()
    total_rooms = sum(h.total_rooms for h in hostels)
    total_beds = sum(h.total_beds for h in hostels)
    occupied_beds = sum(h.occupied_beds for h in hostels)
    available_beds = sum(h.available_beds for h in hostels)
    maintenance_beds = sum(h.maintenance_beds for h in hostels)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0

    recent_occupancies_qs = (
        StudentOccupancy.objects.all()
        .select_related("student", "bed__room__floor__block__hostel")
    )
    if institution:
        recent_occupancies_qs = recent_occupancies_qs.filter(institution=institution)
    recent_occupancies = recent_occupancies_qs.order_by("-created_at")[:8]

    return {
        "hostels": hostels,
        "total_hostels": total_hostels,
        "total_blocks": total_blocks,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "occupied_beds": occupied_beds,
        "available_beds": available_beds,
        "available_operational_beds": available_beds,
        "maintenance_beds": maintenance_beds,
        "occupancy_pct": occupancy_pct,
        "recent_occupancies": recent_occupancies,
    }


def get_dean_dashboard_context(user: User) -> dict:
    """Read-only executive analytics for Dean."""
    institution = user.institution
    hostels_qs = get_hostel_queryset_with_stats(institution=institution)
    hostels = list(hostels_qs)

    total_hostels = len(hostels)
    total_rooms = sum(h.total_rooms for h in hostels)
    total_beds = sum(h.total_beds for h in hostels)
    occupied_beds = sum(h.occupied_beds for h in hostels)
    available_beds = sum(h.available_beds for h in hostels)
    maintenance_beds = sum(h.maintenance_beds for h in hostels)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0

    return {
        "hostels": hostels,
        "total_hostels": total_hostels,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "occupied_beds": occupied_beds,
        "available_beds": available_beds,
        "available_operational_beds": available_beds,
        "maintenance_beds": maintenance_beds,
        "occupancy_pct": occupancy_pct,
    }
