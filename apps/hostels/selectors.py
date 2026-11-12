from django.db.models import Count, Q, F, Value, IntegerField, ExpressionWrapper
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


def get_hostel_queryset_with_stats(institution=None, user=None):
    """
    Fetch hostels with real aggregate database statistics (total rooms, total beds, occupied beds, vacant beds).
    Scopes by institution and, for wardens, by their assigned hostels.
    """
    qs = Hostel.objects.all().select_related("institution")

    if institution:
        qs = qs.filter(institution=institution)

    # If user is a warden (and not admin/chief warden), restrict to assigned hostels
    if user and user.is_warden_role and not (user.is_chief_warden_role or user.is_hostel_admin_role):
        assigned_hostel_ids = WardenAssignment.objects.filter(warden=user).values_list("hostel_id", flat=True)
        qs = qs.filter(id__in=assigned_hostel_ids)

    qs = qs.annotate(
        total_rooms=Count("blocks__floors__rooms", distinct=True),
        total_beds=Count("blocks__floors__rooms__beds", distinct=True),
        occupied_beds=Count(
            "blocks__floors__rooms__beds",
            filter=Q(blocks__floors__rooms__beds__occupancies__status=StudentOccupancy.OccupancyStatus.ACTIVE),
            distinct=True,
        ),
        maintenance_beds=Count(
            "blocks__floors__rooms__beds",
            filter=Q(blocks__floors__rooms__beds__operational_status=OperationalStatus.MAINTENANCE),
            distinct=True,
        ),
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
    """
    qs = (
        Room.objects.all()
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

    qs = qs.annotate(
        total_beds_count=Count("beds", distinct=True),
        occupied_beds_count=Count(
            "beds",
            filter=Q(beds__occupancies__status=StudentOccupancy.OccupancyStatus.ACTIVE),
            distinct=True,
        ),
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


def get_eligible_unallocated_students(institution):
    """Return active students within the institution who currently do NOT have an active bed assignment."""
    active_occupant_ids = StudentOccupancy.objects.filter(
        institution=institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
    ).values_list("student_id", flat=True)

    return (
        User.objects.filter(
            institution=institution,
            is_active=True,
            groups__name="Student",
        )
        .exclude(id__in=active_occupant_ids)
        .order_by("full_name")
    )


def get_vacant_beds(institution, hostel_id=None):
    """Return in-service beds that have no active occupant."""
    occupied_bed_ids = StudentOccupancy.objects.filter(
        institution=institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
    ).values_list("bed_id", flat=True)

    qs = (
        Bed.objects.filter(
            room__floor__block__hostel__institution=institution,
            operational_status=OperationalStatus.IN_SERVICE,
        )
        .exclude(id__in=occupied_bed_ids)
        .select_related("room__floor__block__hostel")
        .order_by("room__floor__block__hostel__name", "room__room_number", "bed_label")
    )

    if hostel_id:
        qs = qs.filter(room__floor__block__hostel_id=hostel_id)
    return qs


# Role-Specific Dashboard Context Selectors
def get_student_dashboard_context(user: User) -> dict:
    occupancy = get_student_active_occupancy(user)
    # Also fetch past occupancy history for the student
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
    available_beds = max(0, total_beds - occupied_beds)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0
        h.available_beds = max(0, h.total_beds - h.occupied_beds)

    # Recent active allocations in assigned hostels
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
    maintenance_beds = sum(h.maintenance_beds for h in hostels)
    available_beds = max(0, total_beds - occupied_beds - maintenance_beds)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    # Compute individual hostel occupancy percentage
    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0
        h.available_beds = max(0, h.total_beds - h.occupied_beds)

    return {
        "hostels": hostels,
        "total_hostels": total_hostels,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "occupied_beds": occupied_beds,
        "available_beds": available_beds,
        "maintenance_beds": maintenance_beds,
        "occupancy_pct": occupancy_pct,
    }


def get_admin_dashboard_context(user: User) -> dict:
    institution = user.institution
    hostels_qs = get_hostel_queryset_with_stats(institution=institution)
    hostels = list(hostels_qs)

    total_hostels = len(hostels)
    total_blocks = Block.objects.filter(hostel__institution=institution).count() if institution else Block.objects.count()
    total_rooms = sum(h.total_rooms for h in hostels)
    total_beds = sum(h.total_beds for h in hostels)
    occupied_beds = sum(h.occupied_beds for h in hostels)
    maintenance_beds = sum(h.maintenance_beds for h in hostels)
    available_beds = max(0, total_beds - occupied_beds - maintenance_beds)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0
        h.available_beds = max(0, h.total_beds - h.occupied_beds)

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
    available_beds = max(0, total_beds - occupied_beds)
    occupancy_pct = round((occupied_beds / total_beds * 100), 1) if total_beds > 0 else 0

    for h in hostels:
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0
        h.available_beds = max(0, h.total_beds - h.occupied_beds)

    return {
        "hostels": hostels,
        "total_hostels": total_hostels,
        "total_rooms": total_rooms,
        "total_beds": total_beds,
        "occupied_beds": occupied_beds,
        "available_beds": available_beds,
        "occupancy_pct": occupancy_pct,
    }
