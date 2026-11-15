from django.db.models import QuerySet

from apps.hostels.models import Bed, Hostel, OperationalStatus, Room

from .models import ExchangeRequest


def get_student_exchange_requests(student) -> QuerySet:
    """Return exchange requests owned by the student, newest first."""
    return (
        ExchangeRequest.objects.filter(student=student)
        .select_related(
            "current_occupancy__bed__room__floor__block__hostel",
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
