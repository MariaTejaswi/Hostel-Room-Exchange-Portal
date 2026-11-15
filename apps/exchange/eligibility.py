from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.hostels.models import Hostel, OperationalStatus, StudentOccupancy

from .models import PolicyRule


@dataclass(frozen=True)
class ExternalStatusResult:
    """Result returned by an institution's fee or disciplinary integration."""

    available: bool
    eligible: bool
    reason: str = ""


class ExternalEligibilityAdapter(Protocol):
    """Interface for fee and disciplinary status integrations."""

    def check_fee_status(self, student) -> ExternalStatusResult:
        ...

    def check_disciplinary_status(self, student) -> ExternalStatusResult:
        ...


class UnavailableExternalEligibilityAdapter:
    """
    Safe default until an institution supplies its P03 fee/disciplinary adapter.

    It never fabricates a status. Policies that require either check therefore
    fail closed with an actionable validation message.
    """

    def check_fee_status(self, student) -> ExternalStatusResult:
        return ExternalStatusResult(
            available=False,
            eligible=False,
            reason="Fee status integration is not configured.",
        )

    def check_disciplinary_status(self, student) -> ExternalStatusResult:
        return ExternalStatusResult(
            available=False,
            eligible=False,
            reason="Disciplinary status integration is not configured.",
        )


def _policy_value(institution, key, default):
    policy = (
        PolicyRule.objects.filter(
            institution=institution,
            key=key,
            is_active=True,
        )
        .only("value")
        .first()
    )
    if policy is None:
        return default
    value = policy.value
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return value


def _policy_bool(institution, key, default=False) -> bool:
    value = _policy_value(institution, key, default)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_request_expiry(*, institution, now=None):
    """Return the configured request expiry, or None when no expiry is configured."""
    now = now or timezone.now()
    value = _policy_value(
        institution,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS,
        30,
    )
    try:
        validity_days = int(value)
    except (TypeError, ValueError):
        raise ValidationError("The request validity policy must contain a whole number of days.")
    if validity_days <= 0:
        return None
    return now + timedelta(days=validity_days)


def validate_exchange_request_eligibility(
    *,
    student,
    occupancy,
    preferences=None,
    status_adapter=None,
    today=None,
):
    """
    Validate all request-entry policies and return the validated occupancy.

    This function is intentionally independent of matching and proposal logic.
    """
    preferences = preferences or {}
    today = today or timezone.localdate()
    status_adapter = status_adapter or UnavailableExternalEligibilityAdapter()
    errors = []

    if not student.is_student_role:
        errors.append("Only students can create exchange requests.")
    if not student.institution_id:
        errors.append("Your account is not associated with an institution.")
    if occupancy is None or occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE:
        errors.append("An active room allocation is required to create an exchange request.")
    elif occupancy.student_id != student.id:
        errors.append("The selected occupancy does not belong to you.")
    elif occupancy.institution_id != student.institution_id:
        errors.append("Your occupancy belongs to a different institution.")

    if occupancy is not None:
        bed = occupancy.bed
        room = bed.room
        hostel = room.floor.block.hostel
        if hostel.institution_id != student.institution_id:
            errors.append("Your current hostel belongs to a different institution.")
        if hostel.status != Hostel.Status.ACTIVE:
            errors.append("Your current hostel is not active.")
        if not room.is_active or room.operational_status != OperationalStatus.IN_SERVICE:
            errors.append("Your current room is not operational.")
        if (
            not bed.is_active
            or bed.operational_status != OperationalStatus.IN_SERVICE
        ):
            errors.append("Your current bed is not operational.")

        eligibility = student.residential_eligibility
        if eligibility not in {
            Hostel.ResidentCategory.MALE,
            Hostel.ResidentCategory.FEMALE,
        }:
            errors.append(
                "Your residential eligibility is not verified by the institution."
            )
        elif hostel.resident_category != eligibility:
            errors.append(
                "Your current hostel category does not match your verified residential eligibility."
            )

        cooling_off_days = _policy_value(
            student.institution,
            PolicyRule.Key.COOLING_OFF_DAYS,
            0,
        )
        try:
            cooling_off_days = int(cooling_off_days)
        except (TypeError, ValueError):
            errors.append("The cooling-off policy must contain a whole number of days.")
        else:
            if cooling_off_days > 0 and today < occupancy.start_date + timedelta(days=cooling_off_days):
                eligible_on = occupancy.start_date + timedelta(days=cooling_off_days)
                errors.append(
                    f"Your accommodation is under a {cooling_off_days}-day cooling-off period "
                    f"until {eligible_on:%Y-%m-%d}."
                )

    preferred_hostel = preferences.get("preferred_hostel")
    preferred_block = preferences.get("preferred_block")
    preferred_floor = preferences.get("preferred_floor")
    preferred_bed = preferences.get("preferred_bed")
    if preferred_hostel is not None:
        if preferred_hostel.institution_id != student.institution_id:
            errors.append("Preferred hostel must belong to your institution.")
        if preferred_hostel.status != Hostel.Status.ACTIVE:
            errors.append("Preferred hostel is not active.")
        if student.residential_eligibility in {
            Hostel.ResidentCategory.MALE,
            Hostel.ResidentCategory.FEMALE,
        } and preferred_hostel.resident_category != student.residential_eligibility:
            errors.append(
                "Preferred hostel does not match your verified residential eligibility."
            )
    if preferred_block is not None:
        if preferred_block.hostel.institution_id != student.institution_id:
            errors.append("Preferred block must belong to your institution.")
        if preferred_hostel is not None and preferred_block.hostel_id != preferred_hostel.id:
            errors.append("Preferred block must belong to the selected hostel.")
    if preferred_floor is not None:
        if preferred_floor.block.hostel.institution_id != student.institution_id:
            errors.append("Preferred floor must belong to your institution.")
        if preferred_block is not None and preferred_floor.block_id != preferred_block.id:
            errors.append("Preferred floor must belong to the selected block.")
    if preferred_bed is not None:
        target_room = preferred_bed.room
        target_floor = target_room.floor
        target_block = target_floor.block
        target_hostel = target_block.hostel
        if preferred_bed.id == occupancy.bed_id:
            errors.append("Preferred bed must differ from your current bed.")
        if (
            not preferred_bed.is_active
            or preferred_bed.operational_status != OperationalStatus.IN_SERVICE
            or not target_room.is_active
            or target_room.operational_status != OperationalStatus.IN_SERVICE
            or not target_floor.is_active
            or not target_block.is_active
            or target_hostel.status != Hostel.Status.ACTIVE
        ):
            errors.append("Preferred bed is not available for exchange.")
        if target_hostel.institution_id != student.institution_id:
            errors.append("Preferred bed must belong to your institution.")
        if target_hostel.resident_category != student.residential_eligibility:
            errors.append("Preferred bed does not match your verified residential eligibility.")
        if preferred_hostel is not None and preferred_hostel.id != target_hostel.id:
            errors.append("Preferred hostel must contain the selected bed.")
        if preferred_block is not None and preferred_block.id != target_block.id:
            errors.append("Preferred block must contain the selected bed.")
        if preferred_floor is not None and preferred_floor.id != target_floor.id:
            errors.append("Preferred floor must contain the selected bed.")

    if _policy_bool(
        student.institution,
        PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
    ):
        fee_status = status_adapter.check_fee_status(student)
        if not fee_status.available:
            errors.append(f"Fee status could not be verified: {fee_status.reason}")
        elif not fee_status.eligible:
            errors.append(f"Fee eligibility check failed: {fee_status.reason}")

    if _policy_bool(
        student.institution,
        PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD,
    ):
        disciplinary_status = status_adapter.check_disciplinary_status(student)
        if not disciplinary_status.available:
            errors.append(
                "Disciplinary status could not be verified: "
                f"{disciplinary_status.reason}"
            )
        elif not disciplinary_status.eligible:
            errors.append(
                f"Disciplinary eligibility check failed: {disciplinary_status.reason}"
            )

    if errors:
        raise ValidationError(errors)
    return occupancy
