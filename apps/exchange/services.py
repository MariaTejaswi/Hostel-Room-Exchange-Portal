from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditLog
from apps.hostels.models import StudentOccupancy

from .eligibility import (
    ExternalEligibilityAdapter,
    get_request_expiry,
    validate_exchange_request_eligibility,
)
from .models import ExchangeRequest


def _assert_student(student):
    if not student.is_authenticated or not student.is_student_role:
        raise ValidationError("Only authenticated students can manage exchange requests.")


def _request_data(data):
    preferred_bed = data.get("preferred_bed")
    hostel = data.get("preferred_hostel")
    block = data.get("preferred_block")
    floor = data.get("preferred_floor")
    room_type = data.get("preferred_room_type", "")
    occupancy_category = data.get("preferred_occupancy_category", "").strip()

    if preferred_bed:
        hostel = preferred_bed.room.floor.block.hostel
        block = preferred_bed.room.floor.block
        floor = preferred_bed.room.floor
        if not room_type:
            room_type = preferred_bed.room.room_type
        if not occupancy_category:
            occupancy_category = preferred_bed.room.occupancy_category or "Standard"

    return {
        "preferred_hostel": hostel,
        "preferred_block": block,
        "preferred_floor": floor,
        "preferred_bed": preferred_bed,
        "preferred_room_type": room_type,
        "preferred_occupancy_category": occupancy_category,
        "preferred_accessibility_attributes": data.get(
            "preferred_accessibility_attributes",
            "",
        ).strip(),
        "reason": data.get("reason", "").strip(),
    }


@transaction.atomic
def create_exchange_request(
    *,
    student,
    data,
    status_adapter: ExternalEligibilityAdapter | None = None,
    now=None,
    ip_address=None,
):
    """Create one validated, institution-scoped student exchange request."""
    _assert_student(student)
    now = now or timezone.now()
    locked_occupancy = (
        StudentOccupancy.objects.select_for_update()
        .select_related("student", "bed__room__floor__block__hostel", "institution")
        .filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .first()
    )
    if locked_occupancy is None:
        raise ValidationError("An active room allocation is required to create an exchange request.")

    if ExchangeRequest.objects.filter(
        student=student,
        status__in=[
            ExchangeRequest.Status.OPEN,
            ExchangeRequest.Status.MATCHED,
        ],
    ).exists():
        raise ValidationError("You already have an active exchange request.")

    values = _request_data(data)
    validate_exchange_request_eligibility(
        student=student,
        occupancy=locked_occupancy,
        preferences=values,
        status_adapter=status_adapter,
        today=now.date(),
    )
    if not values["reason"]:
        raise ValidationError("An exchange reason is required.")

    exchange_request = ExchangeRequest(
        institution=student.institution,
        student=student,
        current_occupancy=locked_occupancy,
        expires_at=get_request_expiry(institution=student.institution, now=now),
        **values,
    )
    exchange_request.full_clean()
    exchange_request.save()
    AuditLog.objects.create(
        acting_user=student,
        institution=student.institution,
        action="EXCHANGE_REQUEST_CREATED",
        target_type="ExchangeRequest",
        target_id=str(exchange_request.id),
        change_summary=f"Created exchange request for {student.full_name}",
        ip_address=ip_address,
    )
    return exchange_request


@transaction.atomic
def update_exchange_request(
    *,
    exchange_request,
    student,
    data,
    status_adapter: ExternalEligibilityAdapter | None = None,
    now=None,
    ip_address=None,
):
    """Edit an owned open exchange request after revalidating current eligibility."""
    _assert_student(student)
    now = now or timezone.now()
    locked_request = (
        ExchangeRequest.objects.select_for_update()
        .get(pk=exchange_request.pk)
    )
    if locked_request.student_id != student.id:
        raise ValidationError("You can only modify your own exchange requests.")
    if locked_request.status != ExchangeRequest.Status.OPEN:
        raise ValidationError("Only open exchange requests can be edited.")

    occupancy = (
        StudentOccupancy.objects.select_for_update()
        .select_related("student", "bed__room__floor__block__hostel", "institution")
        .filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .first()
    )
    if occupancy is None:
        raise ValidationError("An active room allocation is required to edit this request.")

    values = _request_data(data)
    validate_exchange_request_eligibility(
        student=student,
        occupancy=occupancy,
        preferences=values,
        status_adapter=status_adapter,
        today=now.date(),
    )
    if not values["reason"]:
        raise ValidationError("An exchange reason is required.")

    for field, value in values.items():
        setattr(locked_request, field, value)
    locked_request.current_occupancy = occupancy
    locked_request.full_clean()
    locked_request.save(
        update_fields=[
            *values.keys(),
            "current_occupancy",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        acting_user=student,
        institution=student.institution,
        action="EXCHANGE_REQUEST_UPDATED",
        target_type="ExchangeRequest",
        target_id=str(locked_request.id),
        change_summary=f"Updated exchange request for {student.full_name}",
        ip_address=ip_address,
    )
    return locked_request


@transaction.atomic
def withdraw_exchange_request(*, exchange_request, student, ip_address=None):
    """Withdraw an owned open request without deleting its history."""
    _assert_student(student)
    locked_request = ExchangeRequest.objects.select_for_update().get(
        pk=exchange_request.pk
    )
    if locked_request.student_id != student.id:
        raise ValidationError("You can only withdraw your own exchange requests.")
    if locked_request.status != ExchangeRequest.Status.OPEN:
        raise ValidationError("Only open exchange requests can be withdrawn.")

    locked_request.status = ExchangeRequest.Status.WITHDRAWN
    locked_request.save(update_fields=["status", "updated_at"])
    AuditLog.objects.create(
        acting_user=student,
        institution=student.institution,
        action="EXCHANGE_REQUEST_WITHDRAWN",
        target_type="ExchangeRequest",
        target_id=str(locked_request.id),
        change_summary=f"Withdrew exchange request for {student.full_name}",
        ip_address=ip_address,
    )
    return locked_request
