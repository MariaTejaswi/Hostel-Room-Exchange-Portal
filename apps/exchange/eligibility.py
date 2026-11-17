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
    status: str = ""  # e.g., "cleared", "blocked", "unknown", "unavailable"


@dataclass(frozen=True)
class EligibilityCheckItem:
    """Individual item in the exchange eligibility checklist."""

    key: str
    label: str
    passed: bool
    reason: str
    is_blocking: bool = True


@dataclass(frozen=True)
class ExchangeEligibilityReport:
    """Consolidated report on a student's exchange eligibility."""

    is_eligible: bool
    checks: list[EligibilityCheckItem]
    reasons: list[str]
    occupancy: StudentOccupancy | None = None


class ExternalEligibilityAdapter(Protocol):
    """Interface for fee and disciplinary status integrations."""

    def check_fee_status(self, student) -> ExternalStatusResult:
        ...

    def check_disciplinary_status(self, student) -> ExternalStatusResult:
        ...


class UnavailableExternalEligibilityAdapter:
    """
    Authoritative default when an institution has not configured an external adapter.

    It never fabricates clearance. Policies requiring verification fail closed
    with an actionable validation message.
    """

    def check_fee_status(self, student) -> ExternalStatusResult:
        return ExternalStatusResult(
            available=False,
            eligible=False,
            reason="Fee status integration is not configured.",
            status="unavailable",
        )

    def check_disciplinary_status(self, student) -> ExternalStatusResult:
        return ExternalStatusResult(
            available=False,
            eligible=False,
            reason="Disciplinary status integration is not configured.",
            status="unavailable",
        )


REQUIRED_EXCHANGE_POLICY_KEYS = (
    PolicyRule.Key.COOLING_OFF_DAYS,
    PolicyRule.Key.REQUEST_VALIDITY_DAYS,
    PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
    PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD,
)


def get_configured_policy_value(institution, key: str) -> tuple[bool, any]:
    """
    Look up an explicitly configured policy rule for an institution.
    Returns (is_configured: bool, value: any).
    Never fabricates a default.
    """
    if institution is None:
        return False, None
    policy = (
        PolicyRule.objects.filter(
            institution=institution,
            key=key,
            is_active=True,
        )
        .only("value")
        .first()
    )
    if policy is None or policy.value is None:
        return False, None
    val = policy.value
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    return True, val


def get_request_expiry(*, institution, now=None):
    """Return the configured request expiry timestamp, or raise ValidationError if unconfigured."""
    now = now or timezone.now()
    is_conf, val = get_configured_policy_value(
        institution,
        PolicyRule.Key.REQUEST_VALIDITY_DAYS,
    )
    if not is_conf:
        raise ValidationError(
            "The request validity policy has not been configured by the Hostel Administrator."
        )
    try:
        validity_days = int(val)
        if validity_days <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValidationError(
            "The request validity policy must contain a positive whole number of days."
        )
    return now + timedelta(days=validity_days)


def check_student_exchange_eligibility(
    *,
    student,
    occupancy=None,
    preferences=None,
    status_adapter=None,
    today=None,
) -> ExchangeEligibilityReport:
    """
    Evaluate student exchange eligibility against institutional policies and return
    a structured ExchangeEligibilityReport.
    """
    preferences = preferences or {}
    today = today or timezone.localdate()
    status_adapter = status_adapter or UnavailableExternalEligibilityAdapter()
    checks: list[EligibilityCheckItem] = []

    # 1. Institutional Policy Configuration Check
    institution = getattr(student, "institution", None)
    missing_policies = []
    configured_policies = {}
    if institution:
        for pkey in REQUIRED_EXCHANGE_POLICY_KEYS:
            is_conf, val = get_configured_policy_value(institution, pkey)
            if not is_conf:
                missing_policies.append(pkey)
            else:
                configured_policies[pkey] = val
    else:
        missing_policies = list(REQUIRED_EXCHANGE_POLICY_KEYS)

    if missing_policies:
        missing_labels = [p.label for p in PolicyRule.Key if p.value in missing_policies]
        missing_str = ", ".join(missing_labels) if missing_labels else ", ".join(missing_policies)
        checks.append(
            EligibilityCheckItem(
                key="policy_configuration",
                label="Institutional Policy Configuration",
                passed=False,
                reason=(
                    f"Exchange policies ({missing_str}) have not been configured by the "
                    "Hostel Administrator for your institution. Requests cannot be submitted "
                    "until policy setup is completed."
                ),
            )
        )
    else:
        checks.append(
            EligibilityCheckItem(
                key="policy_configuration",
                label="Institutional Policy Configuration",
                passed=True,
                reason="Institution exchange policies are configured and active.",
            )
        )

    # 2. Student Role & Institution
    if not getattr(student, "is_student_role", False):
        checks.append(
            EligibilityCheckItem(
                key="student_role",
                label="Student Account",
                passed=False,
                reason="Only students can create exchange requests.",
            )
        )
    elif not getattr(student, "institution_id", None):
        checks.append(
            EligibilityCheckItem(
                key="student_role",
                label="Institution Scope",
                passed=False,
                reason="Your account is not associated with an institution.",
            )
        )
    else:
        checks.append(
            EligibilityCheckItem(
                key="student_role",
                label="Student Account",
                passed=True,
                reason="Enrolled student account",
            )
        )

    # 3. Active Occupancy
    if occupancy is None:
        occupancy = (
            StudentOccupancy.objects.select_related(
                "student", "bed__room__floor__block__hostel", "institution"
            )
            .filter(
                student=student,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            )
            .first()
        )

    if occupancy is None or occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE:
        checks.append(
            EligibilityCheckItem(
                key="active_occupancy",
                label="Active Room Allocation",
                passed=False,
                reason="An active room allocation is required to create an exchange request.",
            )
        )
    elif occupancy.student_id != student.id:
        checks.append(
            EligibilityCheckItem(
                key="active_occupancy",
                label="Active Room Allocation",
                passed=False,
                reason="The selected occupancy does not belong to you.",
            )
        )
    elif occupancy.institution_id != student.institution_id:
        checks.append(
            EligibilityCheckItem(
                key="active_occupancy",
                label="Active Room Allocation",
                passed=False,
                reason="Your occupancy belongs to a different institution.",
            )
        )
    else:
        checks.append(
            EligibilityCheckItem(
                key="active_occupancy",
                label="Active Room Allocation",
                passed=True,
                reason=f"Allocated to Room {occupancy.bed.room.room_number} · Bed {occupancy.bed.bed_label}",
            )
        )

    # 4. Operational Accommodation & Residential Eligibility
    if occupancy is not None:
        bed = occupancy.bed
        room = bed.room
        hostel = room.floor.block.hostel

        if hostel.institution_id != student.institution_id:
            checks.append(
                EligibilityCheckItem(
                    key="operational_status",
                    label="Operational Accommodation",
                    passed=False,
                    reason="Your current hostel belongs to a different institution.",
                )
            )
        elif hostel.status != Hostel.Status.ACTIVE:
            checks.append(
                EligibilityCheckItem(
                    key="operational_status",
                    label="Operational Accommodation",
                    passed=False,
                    reason="Your current hostel is not active.",
                )
            )
        elif not room.is_active or room.operational_status != OperationalStatus.IN_SERVICE:
            checks.append(
                EligibilityCheckItem(
                    key="operational_status",
                    label="Operational Accommodation",
                    passed=False,
                    reason="Your current room is not operational.",
                )
            )
        elif not bed.is_active or bed.operational_status != OperationalStatus.IN_SERVICE:
            checks.append(
                EligibilityCheckItem(
                    key="operational_status",
                    label="Operational Accommodation",
                    passed=False,
                    reason="Your current bed is not operational.",
                )
            )
        else:
            checks.append(
                EligibilityCheckItem(
                    key="operational_status",
                    label="Operational Accommodation",
                    passed=True,
                    reason="Current accommodation is active and in service",
                )
            )

        eligibility = getattr(student, "residential_eligibility", None)
        if eligibility not in {
            Hostel.ResidentCategory.MALE,
            Hostel.ResidentCategory.FEMALE,
        }:
            checks.append(
                EligibilityCheckItem(
                    key="residential_eligibility",
                    label="Residential Eligibility",
                    passed=False,
                    reason="Your residential eligibility is not verified by the institution.",
                )
            )
        elif hostel.resident_category != eligibility:
            checks.append(
                EligibilityCheckItem(
                    key="residential_eligibility",
                    label="Hostel Category Match",
                    passed=False,
                    reason="Your current hostel category does not match your verified residential eligibility.",
                )
            )
        else:
            checks.append(
                EligibilityCheckItem(
                    key="residential_eligibility",
                    label="Residential Eligibility",
                    passed=True,
                    reason=f"Verified residential status ({student.get_residential_eligibility_display()})",
                )
            )

        # 5. Cooling-off Period
        if PolicyRule.Key.COOLING_OFF_DAYS in configured_policies:
            cooling_off_val = configured_policies[PolicyRule.Key.COOLING_OFF_DAYS]
            try:
                cooling_off_days = int(cooling_off_val)
                if cooling_off_days < 0:
                    raise ValueError()
            except (TypeError, ValueError):
                checks.append(
                    EligibilityCheckItem(
                        key="cooling_off",
                        label="Cooling-off Period",
                        passed=False,
                        reason="The cooling-off policy must contain a non-negative whole number of days.",
                    )
                )
            else:
                if cooling_off_days > 0 and today < occupancy.start_date + timedelta(days=cooling_off_days):
                    eligible_on = occupancy.start_date + timedelta(days=cooling_off_days)
                    checks.append(
                        EligibilityCheckItem(
                            key="cooling_off",
                            label="Cooling-off Period",
                            passed=False,
                            reason=(
                                f"Your accommodation is under a {cooling_off_days}-day cooling-off period "
                                f"until {eligible_on:%Y-%m-%d}."
                            ),
                        )
                    )
                else:
                    checks.append(
                        EligibilityCheckItem(
                            key="cooling_off",
                            label="Cooling-off Period",
                            passed=True,
                            reason=(
                                f"Cooling-off satisfied ({cooling_off_days} days requirement met)"
                                if cooling_off_days > 0
                                else "Cooling-off period is set to 0 days (immediate eligibility)"
                            ),
                        )
                    )

    # 6. Fee Clearance Policy
    if PolicyRule.Key.REQUIRE_FEE_CLEARANCE in configured_policies:
        req_fee = configured_policies[PolicyRule.Key.REQUIRE_FEE_CLEARANCE]
        if isinstance(req_fee, str):
            req_fee = req_fee.lower() in {"1", "true", "yes", "on"}
        if req_fee:
            fee_status = status_adapter.check_fee_status(student)
            if not fee_status.available or fee_status.status in ("unknown", "unavailable"):
                checks.append(
                    EligibilityCheckItem(
                        key="fee_clearance",
                        label="Fee Clearance",
                        passed=False,
                        reason=f"Fee status could not be verified: {fee_status.reason or 'Fee integration unavailable or status unknown.'}",
                    )
                )
            elif not fee_status.eligible or fee_status.status == "blocked":
                checks.append(
                    EligibilityCheckItem(
                        key="fee_clearance",
                        label="Fee Clearance",
                        passed=False,
                        reason=f"Fee eligibility check failed: {fee_status.reason or 'Fee clearance hold.'}",
                    )
                )
            else:
                checks.append(
                    EligibilityCheckItem(
                        key="fee_clearance",
                        label="Fee Clearance",
                        passed=True,
                        reason="Fee clearance verified",
                    )
                )
        else:
            checks.append(
                EligibilityCheckItem(
                    key="fee_clearance",
                    label="Fee Clearance",
                    passed=True,
                    reason="Fee clearance check not mandated by institution policy",
                )
            )

    # 7. Disciplinary Hold Policy
    if PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD in configured_policies:
        req_disc = configured_policies[PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD]
        if isinstance(req_disc, str):
            req_disc = req_disc.lower() in {"1", "true", "yes", "on"}
        if req_disc:
            disc_status = status_adapter.check_disciplinary_status(student)
            if not disc_status.available or disc_status.status in ("unknown", "unavailable"):
                checks.append(
                    EligibilityCheckItem(
                        key="disciplinary_hold",
                        label="Disciplinary Clearance",
                        passed=False,
                        reason=(
                            "Disciplinary status could not be verified: "
                            f"{disc_status.reason or 'Disciplinary integration unavailable or status unknown.'}"
                        ),
                    )
                )
            elif not disc_status.eligible or disc_status.status == "blocked":
                checks.append(
                    EligibilityCheckItem(
                        key="disciplinary_hold",
                        label="Disciplinary Clearance",
                        passed=False,
                        reason=f"Disciplinary eligibility check failed: {disc_status.reason or 'Active disciplinary hold.'}",
                    )
                )
            else:
                checks.append(
                    EligibilityCheckItem(
                        key="disciplinary_hold",
                        label="Disciplinary Clearance",
                        passed=True,
                        reason="No active disciplinary hold",
                    )
                )
        else:
            checks.append(
                EligibilityCheckItem(
                    key="disciplinary_hold",
                    label="Disciplinary Clearance",
                    passed=True,
                    reason="Disciplinary clearance check not mandated by institution policy",
                )
            )

    # 7. Destination / Preferences Validation (if provided)
    preferred_hostel = preferences.get("preferred_hostel")
    preferred_block = preferences.get("preferred_block")
    preferred_floor = preferences.get("preferred_floor")
    preferred_bed = preferences.get("preferred_bed")

    if preferred_hostel is not None:
        if preferred_hostel.institution_id != student.institution_id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_hostel",
                    label="Preferred Hostel",
                    passed=False,
                    reason="Preferred hostel must belong to your institution.",
                )
            )
        if preferred_hostel.status != Hostel.Status.ACTIVE:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_hostel",
                    label="Preferred Hostel",
                    passed=False,
                    reason="Preferred hostel is not active.",
                )
            )
        if getattr(student, "residential_eligibility", None) in {
            Hostel.ResidentCategory.MALE,
            Hostel.ResidentCategory.FEMALE,
        } and preferred_hostel.resident_category != student.residential_eligibility:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_hostel",
                    label="Preferred Hostel Gender Category",
                    passed=False,
                    reason="Preferred hostel does not match your verified residential eligibility.",
                )
            )

    if preferred_block is not None:
        if preferred_block.hostel.institution_id != student.institution_id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_block",
                    label="Preferred Block",
                    passed=False,
                    reason="Preferred block must belong to your institution.",
                )
            )
        if preferred_hostel is not None and preferred_block.hostel_id != preferred_hostel.id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_block",
                    label="Preferred Block Hierarchy",
                    passed=False,
                    reason="Preferred block must belong to the selected hostel.",
                )
            )

    if preferred_floor is not None:
        if preferred_floor.block.hostel.institution_id != student.institution_id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_floor",
                    label="Preferred Floor",
                    passed=False,
                    reason="Preferred floor must belong to your institution.",
                )
            )
        if preferred_block is not None and preferred_floor.block_id != preferred_block.id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_floor",
                    label="Preferred Floor Hierarchy",
                    passed=False,
                    reason="Preferred floor must belong to the selected block.",
                )
            )

    if preferred_bed is not None:
        target_room = preferred_bed.room
        target_floor = target_room.floor
        target_block = target_floor.block
        target_hostel = target_block.hostel

        if occupancy and preferred_bed.id == occupancy.bed_id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed",
                    passed=False,
                    reason="Preferred bed must differ from your current bed.",
                )
            )
        if (
            not preferred_bed.is_active
            or preferred_bed.operational_status != OperationalStatus.IN_SERVICE
            or not target_room.is_active
            or target_room.operational_status != OperationalStatus.IN_SERVICE
            or not target_floor.is_active
            or not target_block.is_active
            or target_hostel.status != Hostel.Status.ACTIVE
        ):
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed Availability",
                    passed=False,
                    reason="Preferred bed is not available for exchange.",
                )
            )
        if target_hostel.institution_id != student.institution_id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed Institution",
                    passed=False,
                    reason="Preferred bed must belong to your institution.",
                )
            )
        if getattr(student, "residential_eligibility", None) in {
            Hostel.ResidentCategory.MALE,
            Hostel.ResidentCategory.FEMALE,
        } and target_hostel.resident_category != student.residential_eligibility:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed Gender Category",
                    passed=False,
                    reason="Preferred bed does not match your verified residential eligibility.",
                )
            )
        if preferred_hostel is not None and preferred_hostel.id != target_hostel.id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed Hostel Match",
                    passed=False,
                    reason="Preferred hostel must contain the selected bed.",
                )
            )
        if preferred_block is not None and preferred_block.id != target_block.id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed Block Match",
                    passed=False,
                    reason="Preferred block must contain the selected bed.",
                )
            )
        if preferred_floor is not None and preferred_floor.id != target_floor.id:
            checks.append(
                EligibilityCheckItem(
                    key="preferred_bed",
                    label="Preferred Bed Floor Match",
                    passed=False,
                    reason="Preferred floor must contain the selected bed.",
                )
            )

    failed_reasons = [c.reason for c in checks if not c.passed and c.is_blocking]
    is_eligible = len(failed_reasons) == 0

    return ExchangeEligibilityReport(
        is_eligible=is_eligible,
        checks=checks,
        reasons=failed_reasons,
        occupancy=occupancy,
    )


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
    report = check_student_exchange_eligibility(
        student=student,
        occupancy=occupancy,
        preferences=preferences,
        status_adapter=status_adapter,
        today=today,
    )
    if not report.is_eligible:
        raise ValidationError(report.reasons)
    return report.occupancy
