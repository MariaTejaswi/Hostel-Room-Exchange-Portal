from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import AuditLog
from apps.hostels.models import Bed, Hostel, OperationalStatus, StudentOccupancy

from apps.hostels.selectors import get_warden_assigned_hostel_ids

from .eligibility import (
    ExternalEligibilityAdapter,
    check_student_exchange_eligibility,
    get_request_expiry,
    validate_exchange_request_eligibility,
)
from .models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)
from .notifications import dispatch_proposal_notification


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
    """
    Create one validated, institution-scoped student exchange or vacant-bed transfer request.
    Server-side vacancy check determines whether this is a vacant-bed transfer or occupied-bed exchange:
    - Vacant bed target: request_type=VACANT_TRANSFER, status=PENDING_APPROVAL.
    - Occupied bed target / preferences: request_type=EXCHANGE, status=OPEN.
    """
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
            ExchangeRequest.Status.PENDING_APPROVAL,
        ],
    ).exists():
        raise ValidationError("You already have an active exchange or transfer request.")

    if ProposalParticipant.objects.filter(student=student, is_active_reservation=True).exists():
        raise ValidationError("You are currently part of an active exchange proposal.")

    values = _request_data(data)
    preferred_bed = values.get("preferred_bed")
    request_type = ExchangeRequest.RequestType.EXCHANGE
    initial_status = ExchangeRequest.Status.OPEN

    if preferred_bed:
        if preferred_bed.id == locked_occupancy.bed_id:
            raise ValidationError("You cannot select your current bed as the destination.")

        if ProposalParticipant.objects.filter(
            Q(proposed_bed=preferred_bed) | Q(current_occupancy__bed=preferred_bed),
            is_active_reservation=True,
        ).exists():
            raise ValidationError("The selected bed is currently reserved by an active exchange proposal.")

        # Determine vacancy server-side from authoritative active occupancy and operational status
        is_occupied = StudentOccupancy.objects.filter(
            bed=preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists()

        if not is_occupied:
            request_type = ExchangeRequest.RequestType.VACANT_TRANSFER
            initial_status = ExchangeRequest.Status.PENDING_APPROVAL

    validate_exchange_request_eligibility(
        student=student,
        occupancy=locked_occupancy,
        preferences=values,
        status_adapter=status_adapter,
        today=now.date(),
    )
    if not values["reason"]:
        raise ValidationError("An exchange or transfer reason is required.")

    exchange_request = ExchangeRequest(
        institution=student.institution,
        student=student,
        current_occupancy=locked_occupancy,
        request_type=request_type,
        status=initial_status,
        expires_at=get_request_expiry(institution=student.institution, now=now),
        **values,
    )
    exchange_request.full_clean()
    exchange_request.save()

    is_vacant_transfer = (request_type == ExchangeRequest.RequestType.VACANT_TRANSFER)
    action_name = "VACANT_TRANSFER_REQUEST_CREATED" if is_vacant_transfer else "EXCHANGE_REQUEST_CREATED"
    summary_text = (
        f"Created vacant-bed transfer request for {student.full_name} targeting bed {preferred_bed.bed_label}"
        if is_vacant_transfer
        else f"Created exchange request for {student.full_name}"
    )

    AuditLog.objects.create(
        acting_user=student,
        institution=student.institution,
        action=action_name,
        target_type="ExchangeRequest",
        target_id=str(exchange_request.id),
        change_summary=summary_text,
        ip_address=ip_address,
    )

    if is_vacant_transfer:
        Notification.objects.create(
            recipient=student,
            notification_type=Notification.NotificationType.APPROVAL_UPDATED,
            message=(
                f"Your vacant-bed transfer request for Bed {preferred_bed.bed_label}, Room {preferred_bed.room.room_number} "
                f"({preferred_bed.room.floor.block.hostel.name}) has been submitted for authority approval. "
                "Note: Your current bed remains allocated until the transfer completes."
            ),
            exchange_request=exchange_request,
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
    """Withdraw an owned open or pending request without deleting its history."""
    _assert_student(student)
    now = now or timezone.now()

    locked_request = ExchangeRequest.objects.get(pk=exchange_request.pk)
    if locked_request.student_id != student.id:
        raise ValidationError("You can only withdraw your own exchange requests.")
    if locked_request.status not in (ExchangeRequest.Status.OPEN, ExchangeRequest.Status.PENDING_APPROVAL):
        if locked_request.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER:
            raise ValidationError("Only pending transfer requests can be withdrawn.")
        raise ValidationError("Only open exchange requests can be withdrawn.")

    # Proactive expiry enforcement: commit status change before raising ValidationError
    if locked_request.expires_at and locked_request.expires_at <= now:
        with transaction.atomic():
            locked_request = ExchangeRequest.objects.select_for_update().get(pk=exchange_request.pk)
            locked_request.status = ExchangeRequest.Status.EXPIRED
            locked_request.save(update_fields=["status", "updated_at"])
            is_vt = (locked_request.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER)
            AuditLog.objects.create(
                acting_user=student,
                institution=student.institution,
                action="VACANT_TRANSFER_EXPIRED" if is_vt else "EXCHANGE_REQUEST_EXPIRED",
                target_type="ExchangeRequest",
                target_id=str(locked_request.id),
                change_summary=f"Proactive expiry of {'vacant-bed transfer' if is_vt else 'exchange'} request for {student.full_name}",
                ip_address=ip_address,
            )
            Notification.objects.create(
                recipient=student,
                notification_type=Notification.NotificationType.REQUEST_EXPIRED,
                message=f"Your {'vacant-bed transfer' if is_vt else 'exchange'} request has expired and cannot be withdrawn.",
                exchange_request=locked_request,
            )
        raise ValidationError(f"This {'vacant-bed transfer' if is_vt else 'exchange'} request has expired and cannot be withdrawn.")

    # Prevent withdrawal if actively reserved in a proposal
    if locked_request.proposal_participations.filter(is_active_reservation=True).exists():
        raise ValidationError("Requests linked to active proposals cannot be withdrawn.")

    with transaction.atomic():
        locked_request = ExchangeRequest.objects.select_for_update().get(pk=exchange_request.pk)
        locked_request.status = ExchangeRequest.Status.WITHDRAWN
        locked_request.save(update_fields=["status", "updated_at"])
        is_vt = (locked_request.request_type == ExchangeRequest.RequestType.VACANT_TRANSFER)
        AuditLog.objects.create(
            acting_user=student,
            institution=student.institution,
            action="VACANT_TRANSFER_WITHDRAWN" if is_vt else "EXCHANGE_REQUEST_WITHDRAWN",
            target_type="ExchangeRequest",
            target_id=str(locked_request.id),
            change_summary=f"Withdrew {'vacant-bed transfer' if is_vt else 'exchange'} request for {student.full_name}",
            ip_address=ip_address,
        )
        return locked_request


@transaction.atomic
def expire_exchange_requests(*, institution=None, now=None) -> tuple[int, list[str]]:
    """
    Expire all open or pending exchange/transfer requests whose expires_at timestamp has passed.
    Idempotent and safe to run on Celery Beat or ad-hoc.
    Returns (count_expired, list_of_request_ids).
    """
    now = now or timezone.now()
    qs = ExchangeRequest.objects.select_for_update().filter(
        status__in=[ExchangeRequest.Status.OPEN, ExchangeRequest.Status.PENDING_APPROVAL],
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
        PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS,
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

    if key in (PolicyRule.Key.COOLING_OFF_DAYS, PolicyRule.Key.REQUEST_VALIDITY_DAYS, PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS):
        try:
            int_val = int(value)
            if key == PolicyRule.Key.REQUEST_VALIDITY_DAYS and int_val < 1:
                raise ValidationError("Request validity must be at least 1 day.")
            if key == PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS and int_val < 1:
                raise ValidationError("Proposal acceptance window must be at least 1 hour.")
            if int_val < 0:
                raise ValidationError(f"{key} must be a non-negative whole number.")
            cleaned_value = {"value": int_val}
        except (ValueError, TypeError) as e:
            if isinstance(e, ValidationError):
                raise
            raise ValidationError(f"{key} must be a valid whole number.")
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


def run_exchange_matching(
    *,
    institution,
    now=None,
    status_adapter=None,
    min_length: int = 2,
    max_length: int = 5,
    max_cycles: int | None = 10_000,
    persist_candidates: bool = False,
):
    """
    Exposed documented service entry point for P04 Module M3: Matching Engine.
    Discovers candidate direct swaps (2-way) and rotation cycles (3-5 way)
    for eligible open requests within the given institution.
    """
    from .matching import find_candidate_matches

    return find_candidate_matches(
        institution=institution,
        now=now,
        status_adapter=status_adapter,
        min_length=min_length,
        max_length=max_length,
        max_cycles=max_cycles,
        persist_candidates=persist_candidates,
    )


# =========================================================================
# Module M4: Multi-Party Acceptance Services
# =========================================================================

def activate_exchange_proposal(
    *,
    proposal_id,
    acting_user=None,
    now=None,
    status_adapter=None,
) -> ExchangeProposal:
    """
    Activate a draft candidate match into an active exchange proposal awaiting participant acceptance.
    
    Revalidates requests (open, unexpired), current occupancies (active, matching bed),
    proposed bed preferences and eligibility, and ensures no conflicting active reservations.
    Uses PostgreSQL transaction with consistent row-lock ordering to prevent concurrency races.
    Sets is_active_reservation=True and marks requests MATCHED without mutating physical occupancy.
    """
    from .matching import room_satisfies_request

    now = now or timezone.now()

    with transaction.atomic():
        try:
            proposal = ExchangeProposal.objects.select_for_update().get(id=proposal_id)
        except ExchangeProposal.DoesNotExist:
            raise ValidationError(f"Exchange proposal {proposal_id} does not exist.")

        # Idempotent repeated activation
        if proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE:
            return proposal

        if proposal.status != ExchangeProposal.Status.DRAFT:
            raise ValidationError(
                f"Only draft exchange proposals can be activated (current status: {proposal.get_status_display()})."
            )

        participants = list(
            ProposalParticipant.objects.filter(proposal=proposal)
            .select_related(
                "student",
                "exchange_request",
                "current_occupancy__bed__room__floor__block__hostel",
                "proposed_bed__room__floor__block__hostel",
            )
            .order_by("sequence_order")
        )

        if len(participants) < 2 or len(participants) > 5:
            raise ValidationError("Exchange proposal must have between 2 and 5 participants.")
        if len(participants) != proposal.cycle_length:
            raise ValidationError(
                f"Participant count ({len(participants)}) does not match cycle length ({proposal.cycle_length})."
            )

        student_ids = sorted(p.student_id for p in participants)
        request_ids = sorted(p.exchange_request_id for p in participants)
        bed_ids = sorted(
            set(
                [p.current_occupancy.bed_id for p in participants]
                + [p.proposed_bed_id for p in participants]
            )
        )

        # Consistent row-lock ordering to prevent deadlocks across concurrent activations
        User = get_user_model()
        locked_students = list(User.objects.select_for_update().filter(id__in=student_ids).order_by("id"))
        locked_requests = list(ExchangeRequest.objects.select_for_update().filter(id__in=request_ids).order_by("id"))
        locked_beds = list(Bed.objects.select_for_update().filter(id__in=bed_ids).order_by("id"))

        if len(locked_requests) != len(request_ids):
            raise ValidationError("One or more exchange requests could not be locked.")

        # 1. Revalidate all requests
        for req in locked_requests:
            if req.institution_id != proposal.institution_id:
                raise ValidationError("Exchange request does not belong to proposal institution.")
            if req.status != ExchangeRequest.Status.OPEN:
                raise ValidationError(f"Request {req.id} is no longer open (status: {req.status}).")
            if req.expires_at and req.expires_at <= now:
                raise ValidationError(f"Request {req.id} has expired.")

        # 2. Revalidate current occupancies
        for p in participants:
            occ = p.current_occupancy
            if (
                occ.status != StudentOccupancy.OccupancyStatus.ACTIVE
                or occ.student_id != p.student_id
                or occ.bed_id != occ.bed.id
            ):
                raise ValidationError(
                    f"Participant {p.student.full_name} current occupancy has changed or is no longer active."
                )

        # 3. Revalidate proposed bed satisfy preferences & eligibility
        for p in participants:
            if not room_satisfies_request(request=p.exchange_request, bed=p.proposed_bed):
                raise ValidationError(
                    f"Proposed accommodation for student {p.student.full_name} no longer satisfies preferences or eligibility."
                )

        # 4. Student eligibility checks (cooling-off, fees, disciplinary holds)
        for p in participants:
            report = check_student_exchange_eligibility(
                student=p.student,
                occupancy=p.current_occupancy,
                status_adapter=status_adapter,
                today=(now.date() if now else None),
            )
            if not report.is_eligible:
                raise ValidationError(
                    f"Participant {p.student.full_name} is no longer eligible: {'; '.join(report.reasons)}"
                )

        # 5. Check no conflicting active reservations in other proposals
        conflicts = (
            ProposalParticipant.objects.filter(is_active_reservation=True)
            .filter(
                Q(student_id__in=student_ids)
                | Q(exchange_request_id__in=request_ids)
                | Q(current_occupancy_id__in=[p.current_occupancy_id for p in participants])
            )
            .exclude(proposal=proposal)
        )
        if conflicts.exists():
            raise ValidationError(
                "One or more participants, requests, or beds are already reserved in another active proposal."
            )

        # 6. Proposal acceptance window policy
        policies = get_institution_exchange_policies(proposal.institution)
        window_hours = policies.get(PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS)
        if window_hours is None:
            raise ValidationError(
                "Proposal acceptance window is not configured for this institution. "
                "A Hostel Administrator must configure this policy before proposals can be activated."
            )
        try:
            window_hours = int(window_hours)
            if window_hours < 1:
                raise ValidationError("Proposal acceptance window must be at least 1 hour.")
        except (ValueError, TypeError):
            raise ValidationError("Proposal acceptance window must be a positive integer.")

        acceptance_deadline = now + timezone.timedelta(hours=window_hours)
        req_expiries = [r.expires_at for r in locked_requests if r.expires_at is not None]
        if req_expiries:
            earliest_req_expiry = min(req_expiries)
            if earliest_req_expiry < acceptance_deadline:
                acceptance_deadline = earliest_req_expiry

        if acceptance_deadline <= now:
            raise ValidationError("Proposal acceptance deadline cannot be in the past.")

        # Mutate workflow state
        proposal.status = ExchangeProposal.Status.PENDING_ACCEPTANCE
        proposal.expires_at = acceptance_deadline
        proposal.save(update_fields=["status", "expires_at", "updated_at"])

        for p in participants:
            p.is_active_reservation = True
            p.response_status = ProposalParticipant.ResponseStatus.PENDING
            p.responded_at = None
            p.save(update_fields=["is_active_reservation", "response_status", "responded_at", "updated_at"])

        for req in locked_requests:
            req.status = ExchangeRequest.Status.MATCHED
            req.save(update_fields=["status", "updated_at"])

        AuditLog.objects.create(
            acting_user=acting_user,
            institution=proposal.institution,
            action="EXCHANGE_PROPOSAL_ACTIVATED",
            target_type="ExchangeProposal",
            target_id=str(proposal.id),
            change_summary=(
                f"Proposal {proposal.id} activated with {proposal.cycle_length} participants. "
                f"Acceptance deadline: {acceptance_deadline.isoformat()}."
            ),
        )

        deadline_str = acceptance_deadline.strftime("%b %d, %Y %H:%M")
        for p in participants:
            dispatch_proposal_notification(
                recipient=p.student,
                notification_type=Notification.NotificationType.ACCEPTANCE_REQUIRED,
                message=(
                    f"You have a new room exchange proposal awaiting your response. "
                    f"All {proposal.cycle_length} participants must accept by {deadline_str}."
                ),
                proposal=proposal,
                exchange_request=p.exchange_request,
                now=now,
            )

        return proposal


def respond_to_proposal(
    *,
    proposal_id,
    student,
    action: str,
    now=None,
    ip_address=None,
) -> tuple[ProposalParticipant, ExchangeProposal]:
    """
    Record an individual participant's acceptance or decline for a pending proposal.
    Unanimous acceptance advances the proposal to PENDING_APPROVAL.
    Any decline immediately cancels the proposal, releases reservations,
    and reverts unexpired requests back to OPEN.
    """
    now = now or timezone.now()
    action = (action or "").strip().lower()
    if action not in ("accept", "decline"):
        raise ValidationError("Action must be 'accept' or 'decline'.")

    _assert_student(student)

    # Initial check outside lock
    try:
        participant = ProposalParticipant.objects.select_related(
            "proposal__institution", "exchange_request", "student"
        ).get(proposal_id=proposal_id, student=student)
    except ProposalParticipant.DoesNotExist:
        raise PermissionDenied("You are not an authorized participant in this exchange proposal.")

    if participant.proposal.institution_id != student.institution_id:
        raise PermissionDenied("Student does not belong to the proposal institution.")

    proposal = participant.proposal

    # Proactive expiry check
    if proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE:
        if proposal.expires_at and proposal.expires_at <= now:
            expire_single_proposal(proposal, now=now)
            raise ValidationError("This exchange proposal has expired and can no longer be accepted or declined.")

    if proposal.status == ExchangeProposal.Status.EXPIRED:
        raise ValidationError("This exchange proposal has expired and can no longer be accepted or declined.")
    if proposal.status == ExchangeProposal.Status.CANCELLED:
        raise ValidationError("This exchange proposal was cancelled and is no longer active.")
    if proposal.status in (
        ExchangeProposal.Status.ACCEPTED,
        ExchangeProposal.Status.PENDING_APPROVAL,
        ExchangeProposal.Status.APPROVED,
        ExchangeProposal.Status.COMPLETED,
    ):
        raise ValidationError("This exchange proposal has already concluded participant acceptance.")
    if proposal.status != ExchangeProposal.Status.PENDING_ACCEPTANCE:
        raise ValidationError(
            f"This exchange proposal is not awaiting acceptance (current status: {proposal.get_status_display()})."
        )

    with transaction.atomic():
        proposal = ExchangeProposal.objects.select_for_update().get(id=proposal_id)
        participant = ProposalParticipant.objects.select_for_update().get(
            proposal=proposal,
            student=student,
        )

        # Re-check under lock
        if proposal.expires_at and proposal.expires_at <= now and proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE:
            expire_single_proposal(proposal, now=now)
            raise ValidationError("This exchange proposal has expired and can no longer be accepted or declined.")

        if proposal.status != ExchangeProposal.Status.PENDING_ACCEPTANCE:
            raise ValidationError(
                f"This exchange proposal is not awaiting acceptance (current status: {proposal.get_status_display()})."
            )

        # Idempotence: if student already gave the same response
        if action == "accept" and participant.response_status == ProposalParticipant.ResponseStatus.ACCEPTED:
            return participant, proposal
        if action == "decline" and participant.response_status == ProposalParticipant.ResponseStatus.DECLINED:
            return participant, proposal

        if participant.response_status != ProposalParticipant.ResponseStatus.PENDING:
            raise ValidationError(f"You have already {participant.response_status} this proposal.")

        if action == "decline":
            participant.response_status = ProposalParticipant.ResponseStatus.DECLINED
            participant.responded_at = now
            participant.save(update_fields=["response_status", "responded_at", "updated_at"])

            proposal.status = ExchangeProposal.Status.CANCELLED
            proposal.save(update_fields=["status", "updated_at"])

            # Release reservations for all participants in this proposal
            ProposalParticipant.objects.filter(proposal=proposal).update(is_active_reservation=False)

            # Revert unexpired requests back to OPEN
            all_parts = list(proposal.participants.select_related("exchange_request", "student"))
            for p in all_parts:
                req = p.exchange_request
                if req.status == ExchangeRequest.Status.MATCHED:
                    has_active_occ = StudentOccupancy.objects.filter(
                        student=p.student,
                        status=StudentOccupancy.OccupancyStatus.ACTIVE,
                    ).exists()
                    is_req_expired = req.expires_at and req.expires_at <= now
                    if not is_req_expired and has_active_occ:
                        req.status = ExchangeRequest.Status.OPEN
                    elif is_req_expired:
                        req.status = ExchangeRequest.Status.EXPIRED
                    req.save(update_fields=["status", "updated_at"])

            AuditLog.objects.create(
                acting_user=student,
                institution=proposal.institution,
                action="EXCHANGE_PROPOSAL_DECLINED",
                target_type="ExchangeProposal",
                target_id=str(proposal.id),
                change_summary=f"Participant declined proposal {proposal.id}. Proposal cancelled and reservations released.",
                ip_address=ip_address,
            )

            for p in all_parts:
                dispatch_proposal_notification(
                    recipient=p.student,
                    notification_type=Notification.NotificationType.ACCEPTANCE_UPDATED,
                    message="Room exchange proposal was cancelled because a participant declined.",
                    proposal=proposal,
                    exchange_request=p.exchange_request,
                    now=now,
                )

            return participant, proposal

        elif action == "accept":
            participant.response_status = ProposalParticipant.ResponseStatus.ACCEPTED
            participant.responded_at = now
            participant.save(update_fields=["response_status", "responded_at", "updated_at"])

            AuditLog.objects.create(
                acting_user=student,
                institution=proposal.institution,
                action="EXCHANGE_PROPOSAL_ACCEPTED",
                target_type="ExchangeProposal",
                target_id=str(proposal.id),
                change_summary=f"Participant accepted proposal {proposal.id}.",
                ip_address=ip_address,
            )

            dispatch_proposal_notification(
                recipient=student,
                notification_type=Notification.NotificationType.ACCEPTANCE_UPDATED,
                message="Your acceptance has been recorded. Waiting for all participants to respond.",
                proposal=proposal,
                exchange_request=participant.exchange_request,
                now=now,
            )

            # Check if all participants accepted
            all_parts = list(proposal.participants.select_related("student", "exchange_request"))
            all_accepted = all(
                p.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
                for p in all_parts
            )

            if all_accepted:
                # Unanimous acceptance advances to PENDING_APPROVAL
                proposal.status = ExchangeProposal.Status.PENDING_APPROVAL
                proposal.accepted_at = now
                proposal.save(update_fields=["status", "accepted_at", "updated_at"])

                AuditLog.objects.create(
                    acting_user=student,
                    institution=proposal.institution,
                    action="EXCHANGE_PROPOSAL_AWAITING_APPROVAL",
                    target_type="ExchangeProposal",
                    target_id=str(proposal.id),
                    change_summary=(
                        f"All {proposal.cycle_length} participants accepted proposal {proposal.id}. "
                        f"Proposal advanced to pending authority approval."
                    ),
                    ip_address=ip_address,
                )

                for p in all_parts:
                    dispatch_proposal_notification(
                        recipient=p.student,
                        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
                        message="All participants have accepted the exchange proposal. It has now been submitted for authority approval.",
                        proposal=proposal,
                        exchange_request=p.exchange_request,
                        now=now,
                    )

            return participant, proposal


def expire_single_proposal(proposal, *, now=None) -> bool:
    """
    Safely expire a pending exchange proposal whose acceptance deadline has passed.
    Releases workflow reservations and reverts unexpired requests back to OPEN.
    Idempotent and atomic.
    """
    now = now or timezone.now()
    with transaction.atomic():
        locked_prop = (
            ExchangeProposal.objects.select_for_update()
            .filter(
                id=proposal.id,
                status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
            )
            .first()
        )
        if not locked_prop:
            return False

        locked_prop.status = ExchangeProposal.Status.EXPIRED
        locked_prop.save(update_fields=["status", "updated_at"])

        ProposalParticipant.objects.filter(proposal=locked_prop).update(is_active_reservation=False)

        all_parts = list(locked_prop.participants.select_related("exchange_request", "student"))
        for p in all_parts:
            req = p.exchange_request
            if req.status == ExchangeRequest.Status.MATCHED:
                has_active_occ = StudentOccupancy.objects.filter(
                    student=p.student,
                    status=StudentOccupancy.OccupancyStatus.ACTIVE,
                ).exists()
                is_req_expired = req.expires_at and req.expires_at <= now
                if not is_req_expired and has_active_occ:
                    req.status = ExchangeRequest.Status.OPEN
                elif is_req_expired:
                    req.status = ExchangeRequest.Status.EXPIRED
                req.save(update_fields=["status", "updated_at"])

        AuditLog.objects.create(
            acting_user=None,
            institution=locked_prop.institution,
            action="EXCHANGE_PROPOSAL_EXPIRED",
            target_type="ExchangeProposal",
            target_id=str(locked_prop.id),
            change_summary=f"Proposal {locked_prop.id} expired at {locked_prop.expires_at}. Reservations released.",
        )

        for p in all_parts:
            dispatch_proposal_notification(
                recipient=p.student,
                notification_type=Notification.NotificationType.PROPOSAL_EXPIRED,
                message="Your room exchange proposal has expired because the acceptance deadline was reached.",
                proposal=locked_prop,
                exchange_request=p.exchange_request,
                now=now,
            )

        return True


def expire_exchange_proposals(*, institution=None, now=None) -> tuple[int, list[str]]:
    """
    Sweep and expire all overdue PENDING_ACCEPTANCE proposals across an institution or globally.
    """
    now = now or timezone.now()
    qs = ExchangeProposal.objects.filter(
        status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
        expires_at__lte=now,
    )
    if institution:
        qs = qs.filter(institution=institution)

    proposal_ids = list(qs.values_list("id", flat=True))
    expired_ids = []

    for prop_id in proposal_ids:
        prop = ExchangeProposal.objects.filter(id=prop_id).first()
        if prop and expire_single_proposal(prop, now=now):
            expired_ids.append(str(prop_id))

    return len(expired_ids), expired_ids


# =========================================================================
# Module M5: Authority Approval Workflow Services
# =========================================================================

def decide_exchange_proposal(
    *,
    proposal_id,
    actor,
    decision: str,
    remarks: str = "",
    now=None,
    ip_address=None,
    status_adapter=None,
) -> tuple[Approval, ExchangeProposal]:
    """
    Record an authoritative approval or rejection decision on an exchange proposal awaiting approval.
    
    Authority Boundaries:
    - Same-hostel proposals: The assigned Warden of that hostel.
    - Cross-hostel proposals: The Chief Warden of the same institution.
    - Deans, Hostel Admins, SysAdmins, and Students are strictly denied decision authority.

    Preconditions verified under consistent row-lock order:
    - Proposal status is PENDING_APPROVAL.
    - Every participant accepted on time.
    - Cycle length and participant count match (2-5).
    - Current student allocations still match.
    - Destination preferences and bed operational statuses remain valid.
    - Participant eligibility (cooling-off, fees, disciplinary holds) verified.
    - Workflow reservations belong to this proposal and are active.
    - Requests are MATCHED and unexpired.
    
    Approved:
    - Transition to APPROVED ("Approved — transfer pending").
    - Retains workflow reservations for M6.
    - Does NOT mutate occupancy or mark requests completed.
    
    Rejected:
    - Transition to REJECTED.
    - Releases this proposal's workflow reservations.
    - Reverts non-expired requests to OPEN; leaves expired requests EXPIRED.
    """
    from .matching import room_satisfies_request

    now = now or timezone.now()
    decision = (decision or "").strip().lower()
    remarks = (remarks or "").strip()

    if decision in ("approved", "approve"):
        decision = "approve"
    elif decision in ("rejected", "reject"):
        decision = "reject"
    else:
        raise ValidationError("Decision must be either 'approve' or 'reject'.")

    if decision == "reject" and not remarks:
        raise ValidationError("A rejection reason/remarks is required when rejecting an exchange proposal.")

    if not actor or not actor.is_authenticated:
        raise PermissionDenied("Authentication is required to record approval decisions.")

    # Strictly deny unauthorized roles
    if actor.is_dean_role:
        raise PermissionDenied("Deans have read-only analytics access and cannot record operational exchange decisions.")
    if actor.is_hostel_admin_role and not (actor.is_warden_role or actor.is_chief_warden_role):
        raise PermissionDenied("Hostel Administrators manage exchange policies, not individual exchange approvals.")
    if actor.is_sysadmin_role and not (actor.is_warden_role or actor.is_chief_warden_role):
        raise PermissionDenied("System Administrators have no operational exchange approval authority.")
    if actor.is_student_role:
        raise PermissionDenied("Students do not have exchange approval authority.")
    if not (actor.is_warden_role or actor.is_chief_warden_role):
        raise PermissionDenied("You do not have exchange approval authority.")

    with transaction.atomic():
        try:
            proposal = ExchangeProposal.objects.select_for_update().get(id=proposal_id)
        except ExchangeProposal.DoesNotExist:
            raise ValidationError(f"Exchange proposal {proposal_id} does not exist.")

        if actor.institution_id != proposal.institution_id:
            raise PermissionDenied("Approver must belong to the proposal institution.")

        # Safe idempotence check: identical repeated submission by the same actor
        existing_approval = Approval.objects.filter(proposal=proposal).first()
        if proposal.status == ExchangeProposal.Status.APPROVED and decision == "approve":
            if existing_approval and existing_approval.authority_user_id == actor.id:
                return existing_approval, proposal
            raise ValidationError("This exchange proposal has already been approved.")

        if proposal.status == ExchangeProposal.Status.REJECTED and decision == "reject":
            if existing_approval and existing_approval.authority_user_id == actor.id:
                return existing_approval, proposal
            raise ValidationError("This exchange proposal has already been rejected.")

        if proposal.status != ExchangeProposal.Status.PENDING_APPROVAL:
            raise ValidationError(
                f"This exchange proposal is not awaiting approval (current status: {proposal.get_status_display()})."
            )

        participants = list(
            ProposalParticipant.objects.filter(proposal=proposal)
            .select_related(
                "student",
                "exchange_request",
                "current_occupancy__bed__room__floor__block__hostel",
                "proposed_bed__room__floor__block__hostel",
            )
            .order_by("sequence_order")
        )

        student_ids = sorted(p.student_id for p in participants)
        request_ids = sorted(p.exchange_request_id for p in participants)
        bed_ids = sorted(
            set(
                [p.current_occupancy.bed_id for p in participants]
                + [p.proposed_bed_id for p in participants]
            )
        )

        # Consistent row-lock ordering to prevent deadlocks across concurrent operations
        User = get_user_model()
        locked_students = list(User.objects.select_for_update().filter(id__in=student_ids).order_by("id"))
        locked_requests = list(ExchangeRequest.objects.select_for_update().filter(id__in=request_ids).order_by("id"))
        locked_beds = list(Bed.objects.select_for_update().filter(id__in=bed_ids).order_by("id"))

        # Authoritative classification from allocation records
        hostel_ids = {
            p.current_occupancy.bed.room.floor.block.hostel_id
            for p in participants
        }
        is_cross_hostel = len(hostel_ids) > 1

        if is_cross_hostel:
            if not actor.is_chief_warden_role:
                raise PermissionDenied("Only the Chief Warden can decide cross-hostel exchange proposals.")
            authority_role = Approval.AuthorityRole.CHIEF_WARDEN
            scope_hostel = None
        else:
            if not actor.is_warden_role:
                raise PermissionDenied("Only the assigned hostel Warden can decide same-hostel exchange proposals.")
            scope_hostel_id = next(iter(hostel_ids))
            assigned_hostel_ids = get_warden_assigned_hostel_ids(actor)
            if scope_hostel_id not in assigned_hostel_ids:
                scope_hostel_obj = participants[0].current_occupancy.bed.room.floor.block.hostel
                raise PermissionDenied(
                    f"You are not assigned to supervise hostel '{scope_hostel_obj.name}'."
                )
            authority_role = Approval.AuthorityRole.WARDEN
            scope_hostel = participants[0].current_occupancy.bed.room.floor.block.hostel

        # 1. Unanimous acceptance verification
        for p in participants:
            if p.response_status != ProposalParticipant.ResponseStatus.ACCEPTED:
                raise ValidationError("Not all participants have accepted this exchange proposal.")
            if p.responded_at and proposal.expires_at and p.responded_at > proposal.expires_at:
                raise ValidationError(
                    f"Participant {p.student.full_name} accepted after the acceptance deadline lapsed."
                )

        # 2. Cycle structure
        if len(participants) < 2 or len(participants) > 5 or len(participants) != proposal.cycle_length:
            raise ValidationError("Proposal participant count is invalid.")

        # 3. Current allocations still match
        for p in participants:
            occ = p.current_occupancy
            if (
                occ.status != StudentOccupancy.OccupancyStatus.ACTIVE
                or occ.student_id != p.student_id
                or occ.bed_id != occ.bed.id
            ):
                raise ValidationError(
                    f"Participant {p.student.full_name} current occupancy has changed or is no longer active."
                )

        # 4. Destination preferences and operational status still valid
        for p in participants:
            if not room_satisfies_request(request=p.exchange_request, bed=p.proposed_bed):
                raise ValidationError(
                    f"Proposed accommodation for student {p.student.full_name} no longer satisfies preferences or operational requirements."
                )

        # 5. Eligibility verification (cooling-off, fees, disciplinary holds)
        for p in participants:
            report = check_student_exchange_eligibility(
                student=p.student,
                occupancy=p.current_occupancy,
                status_adapter=status_adapter,
                today=now.date(),
            )
            if not report.is_eligible:
                raise ValidationError(
                    f"Participant {p.student.full_name} is no longer eligible: {'; '.join(report.reasons)}"
                )

        # 6. Workflow reservations still belong to this proposal
        for p in participants:
            if not p.is_active_reservation:
                raise ValidationError("Workflow reservation for this proposal has been released.")

        other_conflicts = (
            ProposalParticipant.objects.filter(is_active_reservation=True)
            .filter(
                Q(student_id__in=student_ids)
                | Q(exchange_request_id__in=request_ids)
                | Q(current_occupancy_id__in=[p.current_occupancy_id for p in participants])
            )
            .exclude(proposal=proposal)
        )
        if other_conflicts.exists():
            raise ValidationError("A participant or request is reserved by another proposal.")

        # 7. Requests are MATCHED and not expired for approval
        for req in locked_requests:
            if req.status != ExchangeRequest.Status.MATCHED:
                raise ValidationError(f"Exchange request {req.id} is no longer matched.")
            if decision == "approve" and req.expires_at and req.expires_at <= now:
                raise ValidationError(f"Exchange request {req.id} has expired prior to approval decision.")

        # 8. Institution scoping
        for p in participants:
            if p.student.institution_id != proposal.institution_id:
                raise ValidationError("Participant does not belong to the proposal institution.")

        if decision == "approve":
            proposal.status = ExchangeProposal.Status.APPROVED
            proposal.save(update_fields=["status", "updated_at"])

            approval = Approval.objects.create(
                proposal=proposal,
                authority_user=actor,
                authority_role=authority_role,
                scope_hostel=scope_hostel,
                decision=Approval.Decision.APPROVED,
                remarks=remarks,
                decided_at=now,
            )

            AuditLog.objects.create(
                acting_user=actor,
                institution=proposal.institution,
                action="EXCHANGE_PROPOSAL_APPROVED",
                target_type="ExchangeProposal",
                target_id=str(proposal.id),
                change_summary=(
                    f"Proposal {proposal.id} approved by {actor.full_name} ({authority_role}). "
                    f"Transfer execution pending. Remarks: {remarks or 'None'}"
                ),
                ip_address=ip_address,
            )

            for p in participants:
                dispatch_proposal_notification(
                    recipient=p.student,
                    notification_type=Notification.NotificationType.APPROVAL_UPDATED,
                    message="Your room exchange proposal has been approved by the hostel authority. Room transfer execution is pending.",
                    proposal=proposal,
                    exchange_request=p.exchange_request,
                    now=now,
                )

            return approval, proposal

        elif decision == "reject":
            proposal.status = ExchangeProposal.Status.REJECTED
            proposal.save(update_fields=["status", "updated_at"])

            # Release ONLY this proposal's workflow reservations
            ProposalParticipant.objects.filter(proposal=proposal).update(is_active_reservation=False)

            # Revert unexpired requests to OPEN; mark expired requests EXPIRED
            for req in locked_requests:
                if req.status == ExchangeRequest.Status.MATCHED:
                    has_occ = StudentOccupancy.objects.filter(
                        student_id=req.student_id,
                        status=StudentOccupancy.OccupancyStatus.ACTIVE,
                    ).exists()
                    is_expired = req.expires_at and req.expires_at <= now
                    if not is_expired and has_occ:
                        req.status = ExchangeRequest.Status.OPEN
                    elif is_expired:
                        req.status = ExchangeRequest.Status.EXPIRED
                    req.save(update_fields=["status", "updated_at"])

            approval = Approval.objects.create(
                proposal=proposal,
                authority_user=actor,
                authority_role=authority_role,
                scope_hostel=scope_hostel,
                decision=Approval.Decision.REJECTED,
                remarks=remarks,
                decided_at=now,
            )

            AuditLog.objects.create(
                acting_user=actor,
                institution=proposal.institution,
                action="EXCHANGE_PROPOSAL_REJECTED",
                target_type="ExchangeProposal",
                target_id=str(proposal.id),
                change_summary=(
                    f"Proposal {proposal.id} rejected by {actor.full_name} ({authority_role}). "
                    f"Reservations released. Reason: {remarks}"
                ),
                ip_address=ip_address,
            )

            for p in participants:
                dispatch_proposal_notification(
                    recipient=p.student,
                    notification_type=Notification.NotificationType.APPROVAL_UPDATED,
                    message=f"Your room exchange proposal was rejected by the hostel authority. Reason: {remarks}",
                    proposal=proposal,
                    exchange_request=p.exchange_request,
                    now=now,
                )

            return approval, proposal


def approve_and_execute_vacant_transfer(
    *,
    request_id,
    actor,
    remarks: str = "",
    now=None,
    ip_address: str | None = None,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> tuple[Approval, TransferRecord]:
    """
    Approve and atomically execute a vacant-bed transfer in a single PostgreSQL transaction.

    Authority Boundaries:
    - Same-hostel transfer: Assigned Hostel Warden.
    - Cross-hostel transfer: Chief Warden of the same institution.
    - Denied for: Students, Deans, Hostel Admins, SysAdmins, Unassigned Wardens.

    Lock Order (consistent across system):
    1. Sort bed IDs (source bed ID, dest bed ID) and acquire select_for_update() on both Beds.
    2. Acquire select_for_update() on the requesting User (student).
    3. Acquire select_for_update() on the ExchangeRequest.
    4. Acquire select_for_update() on the active StudentOccupancy.
    """
    now = now or timezone.now()

    if not actor.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if getattr(actor, "is_dean_role", False):
        raise PermissionDenied("The Dean of Student Welfare has read-only analytics access and cannot approve transfers.")
    if getattr(actor, "is_sysadmin_role", False):
        raise PermissionDenied("System Administrators have no operational transfer approval authority.")
    if getattr(actor, "is_hostel_admin_role", False):
        raise PermissionDenied("Hostel Administrators manage policy configuration and cannot approve transfers.")
    if getattr(actor, "is_student_role", False):
        raise PermissionDenied("Students cannot approve transfers.")
    if not (getattr(actor, "is_warden_role", False) or getattr(actor, "is_chief_warden_role", False)):
        raise PermissionDenied("You do not have operational authority to approve transfers.")

    # 1. Fetch raw request to identify student, occupancy, and bed IDs
    try:
        raw_req = ExchangeRequest.objects.select_related("current_occupancy", "student", "institution").get(pk=request_id)
    except ExchangeRequest.DoesNotExist:
        raise ValidationError(f"Exchange request {request_id} does not exist.")

    # Proactive expiry enforcement: commit status change before raising ValidationError
    if raw_req.expires_at and raw_req.expires_at <= now:
        with transaction.atomic():
            locked_req = ExchangeRequest.objects.select_for_update().get(id=raw_req.id)
            locked_req.status = ExchangeRequest.Status.EXPIRED
            locked_req.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                acting_user=actor,
                institution=locked_req.institution,
                action="VACANT_TRANSFER_EXPIRED",
                target_type="ExchangeRequest",
                target_id=str(locked_req.id),
                change_summary=f"Transfer request expired prior to approval for {locked_req.student.full_name}",
                ip_address=ip_address,
            )
        raise ValidationError("Transfer request has expired prior to approval decision.")

    with transaction.atomic():
        source_bed_id = raw_req.current_occupancy.bed_id
        dest_bed_id = raw_req.preferred_bed_id
        student_id = raw_req.student_id
        occ_id = raw_req.current_occupancy_id

        # 2. Consistent row-lock ordering:
        # A. Lock Beds in primary-key order
        bed_pks = sorted([source_bed_id, dest_bed_id]) if dest_bed_id else [source_bed_id]
        locked_beds = {
            b.id: b
            for b in Bed.objects.select_for_update()
            .filter(id__in=bed_pks)
        }

        # B. Lock Student (User)
        User = get_user_model()
        locked_student = User.objects.select_for_update().get(id=student_id)

        # C. Lock ExchangeRequest (no outer joins to prevent PostgreSQL NotSupportedError on FOR UPDATE)
        locked_request = ExchangeRequest.objects.select_for_update().get(pk=request_id)

        # D. Lock active StudentOccupancy
        locked_occupancy = (
            StudentOccupancy.objects.select_for_update()
            .filter(id=occ_id)
            .first()
        )

        # 3. Idempotency Check: if already completed, return existing approval and transfer record
        if locked_request.status == ExchangeRequest.Status.COMPLETED:
            existing_record = TransferRecord.objects.filter(exchange_request=locked_request).first()
            existing_approval = Approval.objects.filter(
                exchange_request=locked_request,
                decision=Approval.Decision.APPROVED,
            ).first()
            if existing_record and existing_approval:
                return existing_approval, existing_record

        # 4. Status and Expiry Preconditions
        if locked_request.request_type != ExchangeRequest.RequestType.VACANT_TRANSFER:
            raise ValidationError("This request is not a vacant-bed transfer request.")

        if locked_request.status != ExchangeRequest.Status.PENDING_APPROVAL:
            raise ValidationError(f"Transfer request is in '{locked_request.status}' status and cannot be approved.")



        # 5. Institution and Hierarchy Scoping
        if actor.institution_id != locked_request.institution_id:
            raise PermissionDenied("You cannot approve requests from another institution.")

        source_bed = locked_beds.get(source_bed_id)
        dest_bed = locked_beds.get(dest_bed_id)
        if not dest_bed:
            raise ValidationError("Target bed is missing or invalid.")

        source_hostel = source_bed.room.floor.block.hostel
        dest_hostel = dest_bed.room.floor.block.hostel

        if dest_hostel.institution_id != locked_request.institution_id:
            raise ValidationError("Target bed belongs to a different institution.")

        # 6. Authority Scope Verification
        is_cross_hostel = (source_hostel.id != dest_hostel.id)
        if is_cross_hostel:
            if not getattr(actor, "is_chief_warden_role", False):
                raise PermissionDenied("Cross-hostel transfers require Chief Warden approval.")
            authority_role = Approval.AuthorityRole.CHIEF_WARDEN
            scope_hostel = None
        else:
            assigned_hostels = get_warden_assigned_hostel_ids(actor)
            if source_hostel.id not in assigned_hostels:
                raise PermissionDenied(f"You are not assigned to supervise hostel '{source_hostel.name}'.")
            authority_role = (
                Approval.AuthorityRole.WARDEN
                if getattr(actor, "is_warden_role", False) and not getattr(actor, "is_chief_warden_role", False)
                else Approval.AuthorityRole.CHIEF_WARDEN
            )
            scope_hostel = source_hostel

        # 7. Target Bed Vacancy and Operational Recheck
        if not dest_bed.is_active or dest_bed.operational_status != OperationalStatus.IN_SERVICE:
            raise ValidationError("Target bed is no longer in service.")
        if not dest_bed.room.is_active or dest_bed.room.operational_status != OperationalStatus.IN_SERVICE:
            raise ValidationError("Target room is no longer in service.")
        if not dest_bed.room.floor.is_active or not dest_bed.room.floor.block.is_active or dest_hostel.status != Hostel.Status.ACTIVE:
            raise ValidationError("Target hostel infrastructure is no longer active.")

        if StudentOccupancy.objects.filter(
            bed=dest_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists():
            raise ValidationError("Destination bed is no longer vacant.")

        if ProposalParticipant.objects.filter(
            Q(proposed_bed=dest_bed) | Q(current_occupancy__bed=dest_bed),
            is_active_reservation=True,
        ).exists():
            raise ValidationError("Destination bed is currently reserved by an active exchange proposal.")

        # 8. Source Occupancy Verification
        if (
            locked_occupancy is None
            or locked_occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE
            or locked_occupancy.student_id != locked_student.id
            or locked_occupancy.id != locked_request.current_occupancy_id
        ):
            raise ValidationError("Student current occupancy has changed or is no longer active.")

        # 9. Residential Eligibility & Policy Clearance
        if locked_student.residential_eligibility != dest_hostel.resident_category:
            raise ValidationError("Student residential eligibility does not match destination hostel category.")

        report = check_student_exchange_eligibility(
            student=locked_student,
            occupancy=locked_occupancy,
            status_adapter=status_adapter,
            today=now.date(),
        )
        if not report.is_eligible:
            raise ValidationError(f"Student is no longer eligible: {'; '.join(report.reasons)}")

        # 10. Atomic Execution:
        # A. Close source occupancy
        locked_occupancy.status = StudentOccupancy.OccupancyStatus.TRANSFERRED
        locked_occupancy.end_date = now.date()
        locked_occupancy.release_reason = (
            f"Vacant-bed transfer to {dest_hostel.name}, Room {dest_bed.room.room_number}, Bed {dest_bed.bed_label}"
        )
        locked_occupancy.save(update_fields=["status", "end_date", "release_reason", "updated_at"])

        # B. Create new active occupancy
        new_occupancy = StudentOccupancy.objects.create(
            institution=locked_request.institution,
            student=locked_student,
            bed=dest_bed,
            start_date=now.date(),
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
            source=StudentOccupancy.AllocationSource.VACANT_TRANSFER,
            created_by=actor,
        )

        # C. Record Approval
        approval = Approval.objects.create(
            exchange_request=locked_request,
            authority_user=actor,
            authority_role=authority_role,
            scope_hostel=scope_hostel,
            decision=Approval.Decision.APPROVED,
            remarks=remarks,
            decided_at=now,
        )

        # D. Record Durable TransferRecord
        transfer_record = TransferRecord.objects.create(
            institution=locked_request.institution,
            exchange_request=locked_request,
            approval=approval,
            approved_by=actor,
            remarks=remarks,
            student=locked_student,
            previous_occupancy=locked_occupancy,
            previous_room=source_bed.room,
            previous_bed=source_bed,
            new_occupancy=new_occupancy,
            new_room=dest_bed.room,
            new_bed=dest_bed,
            source_hostel_name=source_hostel.name,
            source_room_number=source_bed.room.room_number,
            source_bed_label=source_bed.bed_label,
            destination_hostel_name=dest_hostel.name,
            destination_room_number=dest_bed.room.room_number,
            destination_bed_label=dest_bed.bed_label,
            transferred_at=now,
        )

        # E. Mark Request Completed
        locked_request.status = ExchangeRequest.Status.COMPLETED
        locked_request.save(update_fields=["status", "updated_at"])

        # F. Durable Audit Log
        AuditLog.objects.create(
            acting_user=actor,
            institution=locked_request.institution,
            action="VACANT_TRANSFER_EXECUTED",
            target_type="ExchangeRequest",
            target_id=str(locked_request.id),
            change_summary=(
                f"Vacant-bed transfer executed for {locked_student.full_name}: "
                f"{source_hostel.name} {source_bed.room.room_number}-{source_bed.bed_label} -> "
                f"{dest_hostel.name} {dest_bed.room.room_number}-{dest_bed.bed_label} by {actor.full_name} ({authority_role}). "
                f"Remarks: {remarks or 'None'}"
            ),
            ip_address=ip_address,
        )

        # G. Student Notification
        Notification.objects.create(
            recipient=locked_student,
            notification_type=Notification.NotificationType.TRANSFER_COMPLETED,
            message=(
                f"Your vacant-bed transfer has been approved and executed by {actor.full_name} ({authority_role}). "
                f"You are now allocated to {dest_hostel.name}, Room {dest_bed.room.room_number}, Bed {dest_bed.bed_label}."
            ),
            exchange_request=locked_request,
        )

        return approval, transfer_record


@transaction.atomic
def reject_vacant_transfer(
    *,
    request_id,
    actor,
    remarks: str,
    now=None,
    ip_address: str | None = None,
) -> tuple[Approval, ExchangeRequest]:
    """
    Reject a vacant-bed transfer request with mandatory remarks.
    Leaves student's current occupancy intact.
    """
    now = now or timezone.now()
    if not remarks or not remarks.strip():
        raise ValidationError("Remarks are required when rejecting a transfer request.")

    if not actor.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if getattr(actor, "is_dean_role", False):
        raise PermissionDenied("The Dean of Student Welfare has read-only analytics access and cannot reject transfers.")
    if getattr(actor, "is_sysadmin_role", False):
        raise PermissionDenied("System Administrators have no operational transfer approval authority.")
    if getattr(actor, "is_hostel_admin_role", False):
        raise PermissionDenied("Hostel Administrators manage policy configuration and cannot reject transfers.")
    if getattr(actor, "is_student_role", False):
        raise PermissionDenied("Students cannot reject transfers.")
    if not (getattr(actor, "is_warden_role", False) or getattr(actor, "is_chief_warden_role", False)):
        raise PermissionDenied("You do not have operational authority to reject transfers.")

    locked_request = ExchangeRequest.objects.select_for_update().get(id=request_id)

    if locked_request.request_type != ExchangeRequest.RequestType.VACANT_TRANSFER:
        raise ValidationError("This request is not a vacant-bed transfer request.")

    if locked_request.status != ExchangeRequest.Status.PENDING_APPROVAL:
        raise ValidationError(f"Transfer request is in '{locked_request.status}' status and cannot be rejected.")

    if actor.institution_id != locked_request.institution_id:
        raise PermissionDenied("You cannot reject requests from another institution.")

    source_hostel = locked_request.current_occupancy.bed.room.floor.block.hostel
    dest_hostel = (
        locked_request.preferred_bed.room.floor.block.hostel
        if locked_request.preferred_bed
        else None
    )

    is_cross_hostel = dest_hostel and (source_hostel.id != dest_hostel.id)
    if is_cross_hostel:
        if not getattr(actor, "is_chief_warden_role", False):
            raise PermissionDenied("Cross-hostel transfers require Chief Warden authority.")
        authority_role = Approval.AuthorityRole.CHIEF_WARDEN
        scope_hostel = None
    else:
        assigned_hostels = get_warden_assigned_hostel_ids(actor)
        if source_hostel.id not in assigned_hostels:
            raise PermissionDenied(f"You are not assigned to supervise hostel '{source_hostel.name}'.")
        authority_role = (
            Approval.AuthorityRole.WARDEN
            if getattr(actor, "is_warden_role", False) and not getattr(actor, "is_chief_warden_role", False)
            else Approval.AuthorityRole.CHIEF_WARDEN
        )
        scope_hostel = source_hostel

    locked_request.status = ExchangeRequest.Status.REJECTED
    locked_request.save(update_fields=["status", "updated_at"])

    approval = Approval.objects.create(
        exchange_request=locked_request,
        authority_user=actor,
        authority_role=authority_role,
        scope_hostel=scope_hostel,
        decision=Approval.Decision.REJECTED,
        remarks=remarks.strip(),
        decided_at=now,
    )

    AuditLog.objects.create(
        acting_user=actor,
        institution=locked_request.institution,
        action="VACANT_TRANSFER_REJECTED",
        target_type="ExchangeRequest",
        target_id=str(locked_request.id),
        change_summary=f"Vacant-bed transfer rejected for {locked_request.student.full_name} by {actor.full_name} ({authority_role}). Reason: {remarks.strip()}",
        ip_address=ip_address,
    )

    Notification.objects.create(
        recipient=locked_request.student,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
        message=f"Your vacant-bed transfer request was rejected by the hostel authority. Reason: {remarks.strip()}",
        exchange_request=locked_request,
    )

    return approval, locked_request

