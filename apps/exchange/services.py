from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditLog
from apps.hostels.models import StudentOccupancy

from .eligibility import (
    ExternalEligibilityAdapter,
    get_request_expiry,
    validate_exchange_request_eligibility,
)
from .models import ExchangeRequest, Notification, PolicyRule


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

    # Pre-check ownership and status
    locked_request = ExchangeRequest.objects.get(pk=exchange_request.pk)
    if locked_request.student_id != student.id:
        raise ValidationError("You can only modify your own exchange requests.")
    if locked_request.status != ExchangeRequest.Status.OPEN:
        raise ValidationError("Only open exchange requests can be edited.")

    # Proactive expiry enforcement: commit status change before raising ValidationError
    if locked_request.expires_at and locked_request.expires_at <= now:
        with transaction.atomic():
            locked_request = ExchangeRequest.objects.select_for_update().get(pk=exchange_request.pk)
            locked_request.status = ExchangeRequest.Status.EXPIRED
            locked_request.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                acting_user=student,
                institution=student.institution,
                action="EXCHANGE_REQUEST_EXPIRED",
                target_type="ExchangeRequest",
                target_id=str(locked_request.id),
                change_summary=f"Proactive expiry of exchange request for {student.full_name}",
                ip_address=ip_address,
            )
            Notification.objects.create(
                recipient=student,
                notification_type=Notification.NotificationType.REQUEST_EXPIRED,
                message="Your exchange request has expired and cannot be modified.",
                exchange_request=locked_request,
            )
        raise ValidationError("This exchange request has expired and cannot be modified.")

    # Active proposal reservation check
    if locked_request.proposal_participations.filter(is_active_reservation=True).exists():
        raise ValidationError("Requests linked to active proposals cannot be modified.")

    with transaction.atomic():
        locked_request = ExchangeRequest.objects.select_for_update().get(pk=exchange_request.pk)
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


def withdraw_exchange_request(*, exchange_request, student, now=None, ip_address=None):
    """Withdraw an owned open request without deleting its history."""
    _assert_student(student)
    now = now or timezone.now()

    locked_request = ExchangeRequest.objects.get(pk=exchange_request.pk)
    if locked_request.student_id != student.id:
        raise ValidationError("You can only withdraw your own exchange requests.")
    if locked_request.status != ExchangeRequest.Status.OPEN:
        raise ValidationError("Only open exchange requests can be withdrawn.")

    # Proactive expiry enforcement: commit status change before raising ValidationError
    if locked_request.expires_at and locked_request.expires_at <= now:
        with transaction.atomic():
            locked_request = ExchangeRequest.objects.select_for_update().get(pk=exchange_request.pk)
            locked_request.status = ExchangeRequest.Status.EXPIRED
            locked_request.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                acting_user=student,
                institution=student.institution,
                action="EXCHANGE_REQUEST_EXPIRED",
                target_type="ExchangeRequest",
                target_id=str(locked_request.id),
                change_summary=f"Proactive expiry of exchange request for {student.full_name}",
                ip_address=ip_address,
            )
            Notification.objects.create(
                recipient=student,
                notification_type=Notification.NotificationType.REQUEST_EXPIRED,
                message="Your exchange request has expired and cannot be withdrawn.",
                exchange_request=locked_request,
            )
        raise ValidationError("This exchange request has expired and cannot be withdrawn.")

    # Prevent withdrawal if actively reserved in a proposal
    if locked_request.proposal_participations.filter(is_active_reservation=True).exists():
        raise ValidationError("Requests linked to active proposals cannot be withdrawn.")

    with transaction.atomic():
        locked_request = ExchangeRequest.objects.select_for_update().get(pk=exchange_request.pk)
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


@transaction.atomic
def expire_exchange_requests(*, institution=None, now=None) -> tuple[int, list[str]]:
    """
    Expire all open exchange requests whose expires_at timestamp has passed.
    Idempotent and safe to run on Celery Beat or ad-hoc.
    Returns (count_expired, list_of_request_ids).
    """
    now = now or timezone.now()
    qs = ExchangeRequest.objects.select_for_update().filter(
        status=ExchangeRequest.Status.OPEN,
        expires_at__lte=now,
    )
    if institution is not None:
        qs = qs.filter(institution=institution)

    expired_ids = []
    for req in qs:
        req.status = ExchangeRequest.Status.EXPIRED
        req.save(update_fields=["status", "updated_at"])
        expired_ids.append(str(req.id))

        AuditLog.objects.create(
            acting_user=None,
            institution=req.institution,
            action="EXCHANGE_REQUEST_EXPIRED",
            target_type="ExchangeRequest",
            target_id=str(req.id),
            change_summary=f"Automated expiry of exchange request for {req.student.full_name}",
        )
        Notification.objects.create(
            recipient=req.student,
            notification_type=Notification.NotificationType.REQUEST_EXPIRED,
            message=f"Your exchange request has expired as of {now.strftime('%Y-%m-%d')}.",
            exchange_request=req,
        )

    return len(expired_ids), expired_ids


def get_institution_exchange_policies(institution) -> dict:
    """
    Return dictionary of policy keys to their configured values for the institution.
    Unconfigured policy keys return None to distinguish unconfigured from explicitly configured false/zero.
    """
    if institution is None:
        return {}
    rules = PolicyRule.objects.filter(institution=institution, is_active=True)
    rule_map = {r.key: r for r in rules}
    result = {}
    for key in (
        PolicyRule.Key.COOLING_OFF_DAYS,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD,
    ):
        if key in rule_map:
            val = rule_map[key].value
            if isinstance(val, dict) and "value" in val:
                val = val["value"]
            result[key] = val
        else:
            result[key] = None
    return result


@transaction.atomic
def update_institution_exchange_policy(
    *,
    acting_user,
    institution,
    key: str,
    value,
    ip_address=None,
):
    """
    Update or create an institution exchange policy rule.
    Strictly restricted to authorized Hostel Administrators for their own institution.
    Students, Wardens, Chief Wardens, Deans, and System Administrators are denied.
    Validates data type and range, auditing actor, institution, before/after values and timestamp.
    """
    if not getattr(acting_user, "is_authenticated", False) or not getattr(acting_user, "is_hostel_admin_role", False):
        raise PermissionDenied("Only Hostel Administrators can manage exchange policy rules.")
    if getattr(acting_user, "institution_id", None) != getattr(institution, "id", None):
        raise PermissionDenied("You cannot configure policies for another institution.")

    if key in (PolicyRule.Key.COOLING_OFF_DAYS, PolicyRule.Key.REQUEST_VALIDITY_DAYS):
        try:
            int_val = int(value)
            if key == PolicyRule.Key.REQUEST_VALIDITY_DAYS and int_val < 1:
                raise ValidationError("Request validity must be at least 1 day.")
            if int_val < 0:
                raise ValidationError(f"{key} must be a non-negative whole number of days.")
            cleaned_value = {"value": int_val}
        except (ValueError, TypeError) as e:
            if isinstance(e, ValidationError):
                raise
            raise ValidationError(f"{key} must be a valid whole number of days.")
    elif key in (PolicyRule.Key.REQUIRE_FEE_CLEARANCE, PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD):
        if isinstance(value, str):
            bool_val = value.lower() in ("true", "1", "yes", "on")
        else:
            bool_val = bool(value)
        cleaned_value = {"value": bool_val}
    else:
        raise ValidationError(f"Invalid policy key: {key}")

    existing_rule = PolicyRule.objects.filter(institution=institution, key=key).first()
    old_value = None
    if existing_rule:
        old_value = existing_rule.value.get("value") if isinstance(existing_rule.value, dict) else existing_rule.value

    rule, created = PolicyRule.objects.select_for_update().get_or_create(
        institution=institution,
        key=key,
        defaults={"value": cleaned_value, "is_active": True},
    )
    if not created:
        rule.value = cleaned_value
        rule.is_active = True
        rule.save(update_fields=["value", "is_active", "updated_at"])

    AuditLog.objects.create(
        acting_user=acting_user,
        institution=institution,
        action="EXCHANGE_POLICY_UPDATED",
        target_type="PolicyRule",
        target_id=str(rule.id),
        change_summary=f"Updated policy {key}: previous={old_value} -> new={cleaned_value['value']}",
        ip_address=ip_address,
    )
    return rule
