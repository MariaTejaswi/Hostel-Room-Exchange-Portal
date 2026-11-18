import logging
import uuid
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import AuditLog, Institution
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
    MatchingJob,
    MatchingOutbox,
    Notification,
    PolicyRule,
    ProposalParticipant,
    SuppressedCycle,
    TransferRecord,
)
from .notifications import dispatch_proposal_notification

logger = logging.getLogger(__name__)


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
    direct_invitation: bool = True,
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

    if ProposalParticipant.objects.filter(
        student=student,
        response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
        is_active_reservation=True,
    ).exists():
        raise ValidationError("You are currently part of an active exchange proposal.")

    values = _request_data(data)
    preferred_bed = values.get("preferred_bed")
    request_type = ExchangeRequest.RequestType.EXCHANGE
    initial_status = ExchangeRequest.Status.OPEN
    matching_status = ExchangeRequest.MatchingStatus.WAITING
    is_direct_occupied_exchange = False
    target_occupant_occ = None

    if preferred_bed:
        if preferred_bed.id == locked_occupancy.bed_id:
            raise ValidationError("You cannot select your current bed as the destination.")

        if ProposalParticipant.objects.filter(
            Q(proposed_bed=preferred_bed) | Q(current_occupancy__bed=preferred_bed),
            is_active_reservation=True,
        ).exists():
            raise ValidationError("The selected bed is currently reserved by an active exchange proposal.")

        # Determine vacancy server-side from authoritative active occupancy and operational status
        target_occupant_occ = StudentOccupancy.objects.select_for_update().filter(
            bed=preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).select_related("student", "bed__room__floor__block__hostel").first()

        if target_occupant_occ is None:
            request_type = ExchangeRequest.RequestType.VACANT_TRANSFER
            initial_status = ExchangeRequest.Status.PENDING_APPROVAL
        elif direct_invitation:
            is_direct_occupied_exchange = True
            recipient = target_occupant_occ.student
            if recipient.id == student.id:
                raise ValidationError("You cannot exchange with yourself.")

            if ProposalParticipant.objects.filter(
                student=recipient,
                response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
                is_active_reservation=True,
            ).exists():
                raise ValidationError("The current occupant of that bed is currently participating in another active exchange proposal.")

            sender_hostel = locked_occupancy.bed.room.floor.block.hostel
            if recipient.residential_eligibility != sender_hostel.resident_category:
                raise ValidationError("The target bed occupant is not eligible for your hostel accommodation.")

            recip_report = check_student_exchange_eligibility(
                student=recipient,
                occupancy=target_occupant_occ,
                status_adapter=status_adapter,
                today=now.date(),
            )
            if not recip_report.is_eligible:
                raise ValidationError(f"The target bed occupant is currently ineligible for exchange: {'; '.join(recip_report.reasons)}")

            request_type = ExchangeRequest.RequestType.EXCHANGE
            initial_status = ExchangeRequest.Status.MATCHED
            matching_status = ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE

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
        matching_status=matching_status,
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
    elif is_direct_occupied_exchange:
        recipient = target_occupant_occ.student
        sender_hostel = locked_occupancy.bed.room.floor.block.hostel
        cycle_signature = f"direct:user:{min(student.id, recipient.id)}:{max(student.id, recipient.id)}"

        if ExchangeProposal.objects.filter(
            cycle_signature=cycle_signature,
            status__in=[
                ExchangeProposal.Status.DRAFT,
                ExchangeProposal.Status.PENDING_ACCEPTANCE,
                ExchangeProposal.Status.ACCEPTED,
                ExchangeProposal.Status.PENDING_APPROVAL,
                ExchangeProposal.Status.APPROVED,
            ],
        ).exists():
            raise ValidationError("An active exchange proposal already exists between you and the occupant.")

        proposal = ExchangeProposal(
            institution=student.institution,
            status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
            cycle_length=2,
            cycle_signature=cycle_signature,
            expires_at=exchange_request.expires_at,
            matched_at=now,
            created_by=student,
        )
        proposal.full_clean()
        proposal.save()

        # Sender: consented by submission
        ProposalParticipant.objects.create(
            proposal=proposal,
            exchange_request=exchange_request,
            student=student,
            current_occupancy=locked_occupancy,
            proposed_bed=preferred_bed,
            sequence_order=0,
            response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
            responded_at=now,
            is_active_reservation=True,
        )

        # Recipient: pending response, no fabricated request
        ProposalParticipant.objects.create(
            proposal=proposal,
            exchange_request=None,
            student=recipient,
            current_occupancy=target_occupant_occ,
            proposed_bed=locked_occupancy.bed,
            sequence_order=1,
            response_status=ProposalParticipant.ResponseStatus.PENDING,
            responded_at=None,
            is_active_reservation=False,
        )

        AuditLog.objects.create(
            acting_user=student,
            institution=student.institution,
            action="EXCHANGE_PROPOSAL_ACTIVATED",
            target_type="ExchangeProposal",
            target_id=str(proposal.id),
            change_summary=f"Direct exchange proposal {proposal.id} created between {student.full_name} and {recipient.full_name}",
            ip_address=ip_address,
        )

        Notification.objects.create(
            recipient=recipient,
            notification_type=Notification.NotificationType.ACCEPTANCE_REQUIRED,
            message=(
                f"{student.full_name} has proposed a direct room exchange with you. "
                f"Proposed accommodation: {sender_hostel.name}, Room {locked_occupancy.bed.room.room_number}, "
                f"Bed {locked_occupancy.bed.bed_label}. Please review and respond before "
                f"{proposal.expires_at.strftime('%b %d, %Y %H:%M') if proposal.expires_at else 'deadline'}."
            ),
            proposal=proposal,
        )

        Notification.objects.create(
            recipient=student,
            notification_type=Notification.NotificationType.ACCEPTANCE_UPDATED,
            message=(
                f"Your direct room exchange proposal for Bed {preferred_bed.bed_label}, "
                f"Room {preferred_bed.room.room_number} ({preferred_bed.room.floor.block.hostel.name}) "
                f"has been submitted. Waiting for occupant {recipient.full_name} to respond."
            ),
            proposal=proposal,
            exchange_request=exchange_request,
        )
    else:
        exchange_request.matching_status = ExchangeRequest.MatchingStatus.WAITING
        exchange_request.save(update_fields=["matching_status"])
        schedule_institution_matching(
            institution_id=student.institution_id,
            trigger_reason="request_created",
            trigger_request_id=exchange_request.id,
        )

    return exchange_request


@transaction.atomic
def activate_direct_occupied_bed_request(
    *,
    exchange_request,
    now=None,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> tuple[bool, str, ExchangeProposal | None]:
    """
    Check if an existing open exchange request targeting a specific bed can be activated as a direct proposal.
    If the target bed has an active occupant who is eligible and available, creates the direct proposal.
    If the occupant has changed, bed is vacant, or cannot be determined, returns False with an explanation.
    """
    now = now or timezone.now()
    if exchange_request.status != ExchangeRequest.Status.OPEN:
        return False, f"Request is not open (current status: {exchange_request.status}).", None
    if not exchange_request.preferred_bed_id:
        return False, "Request does not target a specific bed.", None

    if ProposalParticipant.objects.filter(
        exchange_request=exchange_request,
        is_active_reservation=True,
    ).exists():
        return False, "Request already has an active proposal reservation.", None

    target_occ = StudentOccupancy.objects.select_for_update().filter(
        bed=exchange_request.preferred_bed,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
    ).select_related("student", "bed__room__floor__block__hostel").first()

    if not target_occ:
        return False, "The target bed is no longer occupied. Please reconfirm your preference.", None

    recipient = target_occ.student
    sender = exchange_request.student

    if recipient.id == sender.id:
        return False, "Cannot exchange with self.", None

    if ProposalParticipant.objects.filter(student=recipient, is_active_reservation=True).exists():
        return False, "The occupant of the target bed is participating in another active proposal.", None

    sender_occ = exchange_request.current_occupancy
    if not sender_occ or sender_occ.status != StudentOccupancy.OccupancyStatus.ACTIVE:
        return False, "Your active occupancy is no longer valid.", None

    sender_hostel = sender_occ.bed.room.floor.block.hostel
    if recipient.residential_eligibility != sender_hostel.resident_category:
        return False, "The target bed occupant is not eligible for your hostel.", None

    recip_report = check_student_exchange_eligibility(
        student=recipient,
        occupancy=target_occ,
        status_adapter=status_adapter,
        today=now.date(),
    )
    if not recip_report.is_eligible:
        return False, f"The target bed occupant is currently ineligible: {'; '.join(recip_report.reasons)}", None

    cycle_signature = f"direct:user:{min(sender.id, recipient.id)}:{max(sender.id, recipient.id)}"
    existing_prop = ExchangeProposal.objects.filter(
        cycle_signature=cycle_signature,
        status__in=[
            ExchangeProposal.Status.DRAFT,
            ExchangeProposal.Status.PENDING_ACCEPTANCE,
            ExchangeProposal.Status.ACCEPTED,
            ExchangeProposal.Status.PENDING_APPROVAL,
            ExchangeProposal.Status.APPROVED,
        ],
    ).first()
    if existing_prop:
        return False, "An active proposal already exists for this student pair.", None

    proposal = ExchangeProposal(
        institution=sender.institution,
        status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
        cycle_length=2,
        cycle_signature=cycle_signature,
        expires_at=exchange_request.expires_at or get_request_expiry(institution=sender.institution, now=now),
        matched_at=now,
        created_by=sender,
    )
    proposal.full_clean()
    proposal.save()

    ProposalParticipant.objects.create(
        proposal=proposal,
        exchange_request=exchange_request,
        student=sender,
        current_occupancy=sender_occ,
        proposed_bed=exchange_request.preferred_bed,
        sequence_order=0,
        response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
        responded_at=now,
        is_active_reservation=True,
    )

    ProposalParticipant.objects.create(
        proposal=proposal,
        exchange_request=None,
        student=recipient,
        current_occupancy=target_occ,
        proposed_bed=sender_occ.bed,
        sequence_order=1,
        response_status=ProposalParticipant.ResponseStatus.PENDING,
        responded_at=None,
        is_active_reservation=False,
    )

    exchange_request.status = ExchangeRequest.Status.MATCHED
    exchange_request.matching_status = ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE
    exchange_request.save(update_fields=["status", "matching_status", "updated_at"])

    AuditLog.objects.create(
        acting_user=sender,
        institution=sender.institution,
        action="EXCHANGE_PROPOSAL_ACTIVATED",
        target_type="ExchangeProposal",
        target_id=str(proposal.id),
        change_summary=f"Activated direct exchange proposal {proposal.id} from existing open request for {sender.full_name} and {recipient.full_name}",
    )

    Notification.objects.create(
        recipient=recipient,
        notification_type=Notification.NotificationType.ACCEPTANCE_REQUIRED,
        message=(
            f"{sender.full_name} has proposed a direct room exchange with you. "
            f"Proposed accommodation: {sender_hostel.name}, Room {sender_occ.bed.room.room_number}, "
            f"Bed {sender_occ.bed.bed_label}. Please review and respond before "
            f"{proposal.expires_at.strftime('%b %d, %Y %H:%M') if proposal.expires_at else 'deadline'}."
        ),
        proposal=proposal,
    )

    return True, "Direct exchange proposal activated successfully.", proposal


def update_exchange_request(
    *,
    exchange_request,
    student,
    data,
    status_adapter: ExternalEligibilityAdapter | None = None,
    now=None,
    ip_address=None,
    direct_invitation: bool = True,
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
        locked_request.preference_updated_at = now
        locked_request.matching_status = ExchangeRequest.MatchingStatus.WAITING
        locked_request.full_clean()
        locked_request.save(
            update_fields=[
                *values.keys(),
                "current_occupancy",
                "preference_updated_at",
                "matching_status",
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
        if locked_request.request_type == ExchangeRequest.RequestType.EXCHANGE:
            schedule_institution_matching(
                institution_id=student.institution_id,
                trigger_reason="preferences_updated",
                trigger_request_id=locked_request.id,
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
            req.matching_status = ExchangeRequest.MatchingStatus.PROPOSAL_AVAILABLE
            req.last_matched_at = now
            req.save(update_fields=["status", "matching_status", "last_matched_at", "updated_at"])

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
                if req and req.status == ExchangeRequest.Status.MATCHED:
                    has_active_occ = StudentOccupancy.objects.filter(
                        student=p.student,
                        status=StudentOccupancy.OccupancyStatus.ACTIVE,
                    ).exists()
                    is_req_expired = req.expires_at and req.expires_at <= now
                    if not is_req_expired and has_active_occ:
                        req.status = ExchangeRequest.Status.OPEN
                        req.matching_status = ExchangeRequest.MatchingStatus.WAITING
                    elif is_req_expired:
                        req.status = ExchangeRequest.Status.EXPIRED
                    req.save(update_fields=["status", "matching_status", "updated_at"])

            AuditLog.objects.create(
                acting_user=student,
                institution=proposal.institution,
                action="EXCHANGE_PROPOSAL_DECLINED",
                target_type="ExchangeProposal",
                target_id=str(proposal.id),
                change_summary=f"Participant declined proposal {proposal.id}. Proposal cancelled and reservations released.",
                ip_address=ip_address,
            )

            record_cycle_suppression(proposal=proposal, reason="declined", now=now)
            schedule_institution_matching(
                institution_id=proposal.institution_id,
                trigger_reason="proposal_declined",
                trigger_proposal_id=proposal.id,
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
            if ProposalParticipant.objects.filter(
                student=student,
                response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
                is_active_reservation=True,
            ).exclude(proposal=proposal).exists():
                raise ValidationError("You already hold an active accepted reservation in another exchange proposal.")

            participant.response_status = ProposalParticipant.ResponseStatus.ACCEPTED
            participant.is_active_reservation = True
            participant.responded_at = now
            participant.save(update_fields=["response_status", "is_active_reservation", "responded_at", "updated_at"])

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
            if req and req.status == ExchangeRequest.Status.MATCHED:
                has_active_occ = StudentOccupancy.objects.filter(
                    student=p.student,
                    status=StudentOccupancy.OccupancyStatus.ACTIVE,
                ).exists()
                is_req_expired = req.expires_at and req.expires_at <= now
                if not is_req_expired and has_active_occ:
                    req.status = ExchangeRequest.Status.OPEN
                    req.matching_status = ExchangeRequest.MatchingStatus.WAITING
                elif is_req_expired:
                    req.status = ExchangeRequest.Status.EXPIRED
                req.save(update_fields=["status", "matching_status", "updated_at"])

        AuditLog.objects.create(
            acting_user=None,
            institution=locked_prop.institution,
            action="EXCHANGE_PROPOSAL_EXPIRED",
            target_type="ExchangeProposal",
            target_id=str(locked_prop.id),
            change_summary=f"Proposal {locked_prop.id} expired at {locked_prop.expires_at}. Reservations released.",
        )

        record_cycle_suppression(proposal=locked_prop, reason="expired", now=now)
        schedule_institution_matching(
            institution_id=locked_prop.institution_id,
            trigger_reason="proposal_expired",
            trigger_proposal_id=locked_prop.id,
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
    auto_execute: bool = False,
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
        if proposal.status in (ExchangeProposal.Status.APPROVED, ExchangeProposal.Status.COMPLETED) and decision == "approve":
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
        request_ids = sorted(p.exchange_request_id for p in participants if p.exchange_request_id)
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
                raise PermissionDenied("Cross-hostel proposals can only be decided by the Chief Warden.")
            authority_role = Approval.AuthorityRole.CHIEF_WARDEN
            scope_hostel = None
        else:
            if not actor.is_warden_role:
                raise PermissionDenied("Only the assigned hostel Warden can decide same-hostel exchange proposals.")
            scope_hostel_id = next(iter(hostel_ids))
            scope_hostel = Hostel.objects.get(id=scope_hostel_id)
            assigned_hostel_ids = get_warden_assigned_hostel_ids(actor)
            if scope_hostel_id not in assigned_hostel_ids:
                raise PermissionDenied(f"You are not assigned to supervise hostel '{scope_hostel.name}'.")
            authority_role = Approval.AuthorityRole.WARDEN

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
            if p.exchange_request:
                if not room_satisfies_request(request=p.exchange_request, bed=p.proposed_bed):
                    raise ValidationError(
                        f"Proposed accommodation for student {p.student.full_name} no longer satisfies preferences or operational requirements."
                    )
            else:
                dest_bed = p.proposed_bed
                dest_hostel = dest_bed.room.floor.block.hostel
                if not dest_bed.is_active or dest_bed.operational_status != OperationalStatus.IN_SERVICE:
                    raise ValidationError(f"Proposed bed {dest_bed.bed_label} is no longer in service.")
                if not dest_bed.room.is_active or dest_bed.room.operational_status != "in_service":
                    raise ValidationError(f"Proposed room {dest_bed.room.room_number} is no longer in service.")
                if p.student.residential_eligibility != dest_hostel.resident_category:
                    raise ValidationError(
                        f"Proposed accommodation does not match student {p.student.full_name}'s residential eligibility."
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
            approval, _ = Approval.objects.update_or_create(
                proposal=proposal,
                authority_user=actor,
                authority_role=authority_role,
                scope_hostel=scope_hostel,
                defaults={
                    "decision": Approval.Decision.APPROVED,
                    "remarks": remarks,
                    "decided_at": now,
                },
            )

            proposal.status = ExchangeProposal.Status.APPROVED
            proposal.save(update_fields=["status", "updated_at"])

            AuditLog.objects.create(
                acting_user=actor,
                institution=proposal.institution,
                action="EXCHANGE_PROPOSAL_APPROVED",
                target_type="ExchangeProposal",
                target_id=str(proposal.id),
                change_summary=(
                    f"Proposal {proposal.id} approved by {actor.full_name} ({authority_role}). "
                    f"Remarks: {remarks or 'None'}"
                ),
                ip_address=ip_address,
            )

            if auto_execute:
                execute_exchange_transfer(
                    proposal_id=proposal.id,
                    actor=actor,
                    remarks=remarks,
                    now=now,
                    ip_address=ip_address,
                    status_adapter=status_adapter,
                )
                proposal.refresh_from_db()
            else:
                for p in participants:
                    dispatch_proposal_notification(
                        recipient=p.student,
                        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
                        message="Your room exchange proposal has received official authority approval. Room transfer execution is pending.",
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
                        req.matching_status = ExchangeRequest.MatchingStatus.WAITING
                    elif is_expired:
                        req.status = ExchangeRequest.Status.EXPIRED
                    req.save(update_fields=["status", "matching_status", "updated_at"])

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

            record_cycle_suppression(proposal=proposal, reason="rejected", now=now)
            schedule_institution_matching(
                institution_id=proposal.institution_id,
                trigger_reason="proposal_rejected",
                trigger_proposal_id=proposal.id,
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


def execute_exchange_transfer(
    *,
    proposal_id,
    actor=None,
    remarks: str = "",
    now=None,
    ip_address: str | None = None,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> list[TransferRecord]:
    """
    Atomically execute an approved occupied-bed room exchange proposal.
    Completes direct swaps (2 students) and multi-party rotation cycles (3-5 students).
    All occupancy updates occur atomically in a single PostgreSQL transaction or not at all.

    Canonical Lock Order:
    1. Lock all Beds involved (sorted by ID) without outer joins to prevent PostgreSQL FOR UPDATE errors.
    2. Lock all Student Users involved (sorted by ID).
    3. Lock ExchangeProposal row.
    4. Lock all ExchangeRequests involved (sorted by ID).
    5. Lock all current active StudentOccupancies (sorted by ID).
    """
    now = now or timezone.now()

    if actor is not None:
        if not actor.is_authenticated:
            raise PermissionDenied("Authentication required.")
        if getattr(actor, "is_dean_role", False):
            raise PermissionDenied("The Dean of Student Welfare has read-only analytics access and cannot execute transfers.")
        if getattr(actor, "is_sysadmin_role", False):
            raise PermissionDenied("System Administrators have no operational transfer authority.")
        if getattr(actor, "is_hostel_admin_role", False):
            raise PermissionDenied("Hostel Administrators manage policy configuration and cannot execute transfers.")
        if getattr(actor, "is_student_role", False):
            raise PermissionDenied("Students cannot execute transfers.")
        if not (getattr(actor, "is_warden_role", False) or getattr(actor, "is_chief_warden_role", False)):
            raise PermissionDenied("You do not have operational authority to execute transfers.")

    with transaction.atomic():
        try:
            proposal = ExchangeProposal.objects.select_for_update().get(id=proposal_id)
        except ExchangeProposal.DoesNotExist:
            raise ValidationError(f"Exchange proposal {proposal_id} does not exist.")

        # Safe Idempotency Check: if already completed, return existing transfer records
        if proposal.status == ExchangeProposal.Status.COMPLETED:
            records = list(
                TransferRecord.objects.filter(proposal=proposal)
                .select_related(
                    "student",
                    "previous_occupancy",
                    "new_occupancy",
                    "previous_bed__room__floor__block__hostel",
                    "new_bed__room__floor__block__hostel",
                    "approval",
                )
                .order_by("transferred_at", "id")
            )
            return records

        if proposal.status != ExchangeProposal.Status.APPROVED:
            raise ValidationError(
                f"Exchange proposal #{str(proposal.id)[:8]} is in '{proposal.get_status_display()}' status. "
                f"Only APPROVED proposals can be executed."
            )

        # Verify recorded approval
        approved_records = list(
            Approval.objects.filter(proposal=proposal, decision=Approval.Decision.APPROVED)
            .order_by("-decided_at")
        )
        if not approved_records:
            raise ValidationError("Cannot execute proposal: No recorded authority approval found.")

        # Fetch participants
        participants = list(
            ProposalParticipant.objects.filter(proposal=proposal)
            .select_related("student", "exchange_request", "current_occupancy", "proposed_bed")
            .order_by("sequence_order")
        )
        k = len(participants)
        if k < 2 or k > 5 or k != proposal.cycle_length:
            raise ValidationError(f"Invalid participant count {k} for proposal (cycle length {proposal.cycle_length}).")

        # Validate that all required approvals are satisfied
        prop_hostel_ids = {
            p.current_occupancy.bed.room.floor.block.hostel_id
            for p in participants
        }
        is_cross_hostel = len(prop_hostel_ids) > 1

        if is_cross_hostel:
            has_cw = any(a.authority_role == Approval.AuthorityRole.CHIEF_WARDEN for a in approved_records)
            if not has_cw:
                raise ValidationError("Cannot execute cross-hostel proposal: Chief Warden approval is required.")
            approval = next(a for a in approved_records if a.authority_role == Approval.AuthorityRole.CHIEF_WARDEN)
        else:
            single_hostel_id = next(iter(prop_hostel_ids))
            warden_approval = next(
                (a for a in approved_records if a.authority_role == Approval.AuthorityRole.WARDEN and a.scope_hostel_id == single_hostel_id),
                None
            )
            approval = warden_approval or approved_records[0]

        acting_user = actor or approval.authority_user
        if not acting_user:
            raise PermissionDenied("No authorized acting user found to execute this transfer.")

        if acting_user.institution_id != proposal.institution_id:
            raise PermissionDenied("Approver/executor must belong to the proposal institution.")

        # Collect and sort IDs for global row lock ordering
        student_ids = sorted(p.student_id for p in participants)
        request_ids = sorted(p.exchange_request_id for p in participants if p.exchange_request_id)
        src_bed_ids = [p.current_occupancy.bed_id for p in participants]
        dest_bed_ids = [p.proposed_bed_id for p in participants]
        all_bed_ids = sorted(set(src_bed_ids + dest_bed_ids))
        occ_ids = sorted(p.current_occupancy_id for p in participants)

        # Distinct check: 2 to 5 distinct students and distinct source beds
        if len(set(student_ids)) != k:
            raise ValidationError("Proposal contains duplicate students.")
        if len(set(src_bed_ids)) != k:
            raise ValidationError("Proposal contains duplicate source beds.")
        if len(set(dest_bed_ids)) != k:
            raise ValidationError("Proposal contains duplicate proposed destination beds.")

        # Closed permutation verification: {dest_beds} == {src_beds}
        if set(dest_bed_ids) != set(src_bed_ids):
            raise ValidationError("Proposal destination beds do not form a closed cycle permutation of source beds.")

        # No self-swaps: B_dest != B_src for every participant
        for p in participants:
            if p.current_occupancy.bed_id == p.proposed_bed_id:
                raise ValidationError(f"Participant {p.student.full_name} is assigned to their own bed (no self-swaps permitted).")

        # Global row lock acquisition in canonical order:
        # 1. Beds (NO outer joins on select_for_update)
        locked_beds = {
            b.id: b
            for b in Bed.objects.select_for_update()
            .filter(id__in=all_bed_ids)
        }
        # 2. Users (students)
        User = get_user_model()
        locked_students = {
            u.id: u
            for u in User.objects.select_for_update()
            .filter(id__in=student_ids)
        }
        # 3. ExchangeRequests (NO outer joins on select_for_update)
        locked_requests = {
            r.id: r
            for r in ExchangeRequest.objects.select_for_update()
            .filter(id__in=request_ids)
        }
        # 4. StudentOccupancies (current active occupancies)
        locked_occupancies = {
            o.id: o
            for o in StudentOccupancy.objects.select_for_update()
            .filter(id__in=occ_ids)
        }

        # Validate Authority Scope
        hostel_ids = {
            locked_beds[p.current_occupancy.bed_id].room.floor.block.hostel_id
            for p in participants
        }
        is_cross_hostel = len(hostel_ids) > 1

        if is_cross_hostel:
            if not getattr(acting_user, "is_chief_warden_role", False):
                raise PermissionDenied("Only the Chief Warden can execute cross-hostel exchange transfers.")
        else:
            if not getattr(acting_user, "is_warden_role", False) and not getattr(acting_user, "is_chief_warden_role", False):
                raise PermissionDenied("Only the assigned hostel Warden or Chief Warden can execute same-hostel exchange transfers.")
            if getattr(acting_user, "is_warden_role", False) and not getattr(acting_user, "is_chief_warden_role", False):
                assigned_hostels = get_warden_assigned_hostel_ids(acting_user)
                single_hostel_id = next(iter(hostel_ids))
                if single_hostel_id not in assigned_hostels:
                    raise PermissionDenied("You are not assigned to supervise this hostel.")

        # 1. Unanimous acceptance verification
        for p in participants:
            if p.response_status != ProposalParticipant.ResponseStatus.ACCEPTED:
                raise ValidationError(f"Participant {p.student.full_name} has not accepted this proposal.")
            if p.responded_at and proposal.expires_at and p.responded_at > proposal.expires_at:
                raise ValidationError(f"Participant {p.student.full_name} accepted after the acceptance deadline.")

        # 2. Workflow reservations still held by this proposal
        for p in participants:
            if not p.is_active_reservation:
                raise ValidationError("Workflow reservation for this proposal has been released.")

        foreign_conflicts = (
            ProposalParticipant.objects.filter(is_active_reservation=True)
            .filter(
                Q(student_id__in=student_ids)
                | Q(exchange_request_id__in=request_ids)
                | Q(current_occupancy_id__in=occ_ids)
            )
            .exclude(proposal=proposal)
        )
        if foreign_conflicts.exists():
            raise ValidationError("A participant, request, or bed is reserved by another proposal.")

        # 3. Current active occupancy snapshot check
        for p in participants:
            occ = locked_occupancies.get(p.current_occupancy_id)
            if not occ or occ.status != StudentOccupancy.OccupancyStatus.ACTIVE or occ.student_id != p.student_id:
                raise ValidationError(
                    f"Participant {p.student.full_name}'s current occupancy has changed or is no longer active."
                )
            if occ.bed_id != p.current_occupancy.bed_id:
                raise ValidationError(
                    f"Participant {p.student.full_name}'s allocated bed has changed since proposal creation."
                )

        # 4. Destination beds and rooms operational status, category matching, policy eligibility
        for p in participants:
            dest_bed = locked_beds[p.proposed_bed_id]
            dest_room = dest_bed.room
            dest_hostel = dest_room.floor.block.hostel

            if not dest_bed.is_active or dest_bed.operational_status != OperationalStatus.IN_SERVICE:
                raise ValidationError(f"Destination bed {dest_bed.bed_label} in Room {dest_room.room_number} is no longer in service.")
            if not dest_room.is_active or dest_room.operational_status != OperationalStatus.IN_SERVICE:
                raise ValidationError(f"Destination room {dest_room.room_number} is no longer in service.")
            if not dest_room.floor.is_active or not dest_room.floor.block.is_active or dest_hostel.status != Hostel.Status.ACTIVE:
                raise ValidationError(f"Destination hostel {dest_hostel.name} infrastructure is no longer active.")

            student = locked_students[p.student_id]
            if student.residential_eligibility != dest_hostel.resident_category:
                raise ValidationError(
                    f"Student {student.full_name} residential eligibility does not match destination hostel {dest_hostel.name} category."
                )

            # Check eligibility policies (cooling off, fees, disciplinary holds)
            report = check_student_exchange_eligibility(
                student=student,
                occupancy=locked_occupancies[p.current_occupancy_id],
                status_adapter=status_adapter,
                today=now.date(),
            )
            if not report.is_eligible:
                raise ValidationError(f"Student {student.full_name} is no longer eligible: {'; '.join(report.reasons)}")

        # 5. Requests validation
        for req in locked_requests.values():
            if req.status not in (ExchangeRequest.Status.MATCHED, ExchangeRequest.Status.PENDING_APPROVAL):
                raise ValidationError(f"Exchange request {req.id} is in status '{req.status}', expected MATCHED.")

        # ATOMIC OCCUPANCY MUTATION:
        # Step A: Close ALL current occupancies in the cycle
        for p in participants:
            old_occ = locked_occupancies[p.current_occupancy_id]
            dest_bed = locked_beds[p.proposed_bed_id]
            dest_hostel = dest_bed.room.floor.block.hostel
            old_occ.status = StudentOccupancy.OccupancyStatus.TRANSFERRED
            old_occ.end_date = now.date()
            cycle_desc = "direct swap" if k == 2 else f"{k}-party rotation"
            old_occ.release_reason = (
                f"Completed room exchange {cycle_desc} (Proposal #{str(proposal.id)[:8]}) to "
                f"{dest_hostel.name}, Room {dest_bed.room.room_number}, Bed {dest_bed.bed_label}"
            )
            old_occ.save(update_fields=["status", "end_date", "release_reason", "updated_at"])

        # Step B: Create ALL new active occupancies
        allocation_source = (
            StudentOccupancy.AllocationSource.DIRECT_SWAP
            if k == 2
            else StudentOccupancy.AllocationSource.MULTI_SWAP
        )
        new_occupancies = {}
        for p in participants:
            student = locked_students[p.student_id]
            dest_bed = locked_beds[p.proposed_bed_id]
            new_occ = StudentOccupancy.objects.create(
                institution=proposal.institution,
                student=student,
                bed=dest_bed,
                start_date=now.date(),
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
                source=allocation_source,
                created_by=acting_user,
            )
            new_occupancies[p.id] = new_occ

        # Step C: Create immutable TransferRecord for each participant
        transfer_records = []
        for p in participants:
            student = locked_students[p.student_id]
            src_bed = locked_beds[p.current_occupancy.bed_id]
            dest_bed = locked_beds[p.proposed_bed_id]
            old_occ = locked_occupancies[p.current_occupancy_id]
            new_occ = new_occupancies[p.id]

            rec = TransferRecord.objects.create(
                institution=proposal.institution,
                exchange_request=locked_requests.get(p.exchange_request_id),
                proposal=proposal,
                participant=p,
                approval=approval,
                approved_by=acting_user,
                remarks=remarks or approval.remarks,
                student=student,
                previous_occupancy=old_occ,
                previous_room=src_bed.room,
                previous_bed=src_bed,
                new_occupancy=new_occ,
                new_room=dest_bed.room,
                new_bed=dest_bed,
                source_hostel_name=src_bed.room.floor.block.hostel.name,
                source_room_number=src_bed.room.room_number,
                source_bed_label=src_bed.bed_label,
                destination_hostel_name=dest_bed.room.floor.block.hostel.name,
                destination_room_number=dest_bed.room.room_number,
                destination_bed_label=dest_bed.bed_label,
                transferred_at=now,
            )
            transfer_records.append(rec)

        # Step D: Update Proposal and Requests status
        proposal.status = ExchangeProposal.Status.COMPLETED
        proposal.save(update_fields=["status", "updated_at"])

        for req in locked_requests.values():
            req.status = ExchangeRequest.Status.COMPLETED
            req.save(update_fields=["status", "updated_at"])

        # Step E: Release workflow reservations
        ProposalParticipant.objects.filter(proposal=proposal).update(is_active_reservation=False)

        # Step F: Audit log
        AuditLog.objects.create(
            acting_user=acting_user,
            institution=proposal.institution,
            action="EXCHANGE_TRANSFER_EXECUTED",
            target_type="ExchangeProposal",
            target_id=str(proposal.id),
            change_summary=(
                f"Occupancy transfer executed for proposal {proposal.id} ({k}-student cycle) "
                f"by {acting_user.full_name} ({approval.authority_role}). "
                f"All allocations updated atomically. Remarks: {remarks or approval.remarks or 'None'}"
            ),
            ip_address=ip_address,
        )

        # Step G: Dispatch TRANSFER_COMPLETED notifications to all participants
        for rec in transfer_records:
            Notification.objects.create(
                recipient=rec.student,
                notification_type=Notification.NotificationType.TRANSFER_COMPLETED,
                message=(
                    f"Your room exchange proposal #{str(proposal.id)[:8]} has been approved and completed. "
                    f"You have been successfully transferred to {rec.destination_hostel_name}, "
                    f"Room {rec.destination_room_number}, Bed {rec.destination_bed_label}."
                ),
                proposal=proposal,
                exchange_request=rec.exchange_request,
            )

        return transfer_records


def approve_and_execute_exchange_proposal(
    *,
    proposal_id,
    actor,
    remarks: str = "",
    now=None,
    ip_address: str | None = None,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> tuple[Approval, ExchangeProposal, list[TransferRecord]]:
    """
    Approve an exchange proposal and execute all participant room transfers atomically.
    Convenience wrapper that runs decide_exchange_proposal with auto_execute=True.
    """
    approval, proposal = decide_exchange_proposal(
        proposal_id=proposal_id,
        actor=actor,
        decision="approved",
        remarks=remarks,
        now=now,
        ip_address=ip_address,
        status_adapter=status_adapter,
        auto_execute=True,
    )
    records = list(TransferRecord.objects.filter(proposal=proposal).order_by("transferred_at", "id"))
    return approval, proposal, records


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


# =========================================================================
# Automatic Background Matching & Proposal Activation Services
# =========================================================================

def record_cycle_suppression(*, proposal, reason: str, now=None) -> SuppressedCycle | None:
    """
    Records cycle signature suppression snapshotting each participant's request timestamp.
    Prevents the matching engine from repeatedly generating the exact same rejected/expired/declined cycle.
    """
    now = now or timezone.now()
    if not proposal or not proposal.cycle_signature:
        return None

    participants = list(proposal.participants.select_related("exchange_request"))
    if not participants:
        return None

    revisions = {}
    for p in participants:
        req = p.exchange_request
        if req:
            ts = req.preference_updated_at or req.updated_at or now
            revisions[str(req.id)] = ts.isoformat()
        else:
            revisions[f"user:{p.student_id}"] = (p.updated_at or now).isoformat()

    suppressed = SuppressedCycle.objects.create(
        institution=proposal.institution,
        cycle_signature=proposal.cycle_signature,
        proposal=proposal,
        reason=reason,
        request_revisions=revisions,
        is_active=True,
    )
    return suppressed


def is_candidate_suppressed(candidate, institution) -> bool:
    """
    Check if a candidate match is suppressed based on its cycle signature and participant request revisions.
    If any participant updated preferences since the suppression occurred, the suppression is lifted.
    """
    suppressions = list(
        SuppressedCycle.objects.filter(
            institution=institution,
            cycle_signature=candidate.cycle_signature,
            is_active=True,
        )
    )
    if not suppressions:
        return False

    req_ids = [uuid.UUID(rid) if isinstance(rid, str) else rid for rid in candidate.request_ids]
    requests = {str(r.id): r for r in ExchangeRequest.objects.filter(id__in=req_ids)}

    for supp in suppressions:
        has_new_revision = False
        revisions = supp.request_revisions or {}
        for rid_str, recorded_ts_str in revisions.items():
            req = requests.get(rid_str)
            if not req:
                has_new_revision = True
                break
            current_ts = req.preference_updated_at or req.updated_at
            try:
                recorded_dt = datetime.fromisoformat(recorded_ts_str)
                if current_ts > recorded_dt:
                    has_new_revision = True
                    break
            except (ValueError, TypeError):
                has_new_revision = True
                break

        if has_new_revision:
            supp.is_active = False
            supp.save(update_fields=["is_active", "updated_at"])
        else:
            return True

    return False


def _candidate_sort_key(candidate, request_map):
    """
    Deterministic ordering for candidate matches.
    Implementation choice for fairness:
    1. min_submitted_at: Prioritize cycles that help students who have waited the longest (FIFO).
    2. cycle_length: Prefer shorter cycles (direct 2-way swaps over 5-way rotations) for higher completion probability.
    3. avg_submitted_at: Secondary fairness tie-breaker for group average wait time.
    4. cycle_signature: Stable lexicographical tie-breaker.
    """
    reqs = [request_map[rid] for rid in candidate.request_ids if rid in request_map]
    if not reqs:
        return (timezone.now(), candidate.cycle_length, 0.0, candidate.cycle_signature)

    sub_times = [r.submitted_at for r in reqs]
    min_sub = min(sub_times)
    avg_sub_timestamp = sum(r.submitted_at.timestamp() for r in reqs) / len(reqs)
    return (min_sub, candidate.cycle_length, avg_sub_timestamp, candidate.cycle_signature)


def select_and_activate_candidates(
    *,
    candidates,
    institution,
    request_map,
    now=None,
    status_adapter=None,
) -> list[ExchangeProposal]:
    """
    Select non-overlapping candidates according to deterministic priority order
    and activate them using activate_exchange_proposal.
    Ensures no double-booking of students, requests, or beds across proposals.
    """
    from .matching import ACTIVE_PROPOSAL_STATUSES

    now = now or timezone.now()
    sorted_candidates = sorted(candidates, key=lambda c: _candidate_sort_key(c, request_map))

    pass_reserved_request_ids = set()
    pass_reserved_student_ids = set()
    pass_reserved_bed_ids = set()

    active_reservations = ProposalParticipant.objects.filter(
        proposal__institution=institution,
        is_active_reservation=True,
    ).select_related("current_occupancy", "proposed_bed")

    for p in active_reservations:
        pass_reserved_request_ids.add(p.exchange_request_id)
        pass_reserved_student_ids.add(p.student_id)
        if p.current_occupancy:
            pass_reserved_bed_ids.add(p.current_occupancy.bed_id)
        if p.proposed_bed_id:
            pass_reserved_bed_ids.add(p.proposed_bed_id)

    activated_proposals = []

    for candidate in sorted_candidates:
        if ExchangeProposal.objects.filter(
            institution=institution,
            cycle_signature=candidate.cycle_signature,
            status__in=ACTIVE_PROPOSAL_STATUSES,
        ).exists():
            continue

        cand_request_ids = set(uuid.UUID(rid) if isinstance(rid, str) else rid for rid in candidate.request_ids)
        cand_student_ids = set(p.student_id for p in candidate.participants)
        cand_bed_ids = set(p.current_bed_id for p in candidate.participants) | set(p.proposed_bed_id for p in candidate.participants)

        if cand_request_ids & pass_reserved_request_ids:
            continue
        if cand_student_ids & pass_reserved_student_ids:
            continue
        if cand_bed_ids & pass_reserved_bed_ids:
            continue

        valid_requests = True
        for rid in candidate.request_ids:
            req = request_map.get(rid)
            if not req or req.status != ExchangeRequest.Status.OPEN:
                valid_requests = False
                break
            if req.expires_at and req.expires_at <= now:
                valid_requests = False
                break
        if not valid_requests:
            continue

        try:
            with transaction.atomic():
                expiry_values = [
                    request_map[rid].expires_at
                    for rid in candidate.request_ids
                    if request_map.get(rid) and request_map[rid].expires_at is not None
                ]
                earliest_expiry = min(expiry_values) if expiry_values else None

                draft_proposal = ExchangeProposal.objects.create(
                    institution=institution,
                    status=ExchangeProposal.Status.DRAFT,
                    cycle_length=candidate.cycle_length,
                    cycle_signature=candidate.cycle_signature,
                    expires_at=earliest_expiry,
                    matched_at=now,
                )
                for seq_order, part in enumerate(candidate.participants):
                    req = request_map[part.request_id]
                    ProposalParticipant.objects.create(
                        proposal=draft_proposal,
                        exchange_request=req,
                        student=req.student,
                        current_occupancy=req.current_occupancy,
                        proposed_bed_id=part.proposed_bed_id,
                        sequence_order=seq_order,
                        is_active_reservation=False,
                    )

                activated = activate_exchange_proposal(
                    proposal_id=draft_proposal.id,
                    now=now,
                    status_adapter=status_adapter,
                )
                activated_proposals.append(activated)

                pass_reserved_request_ids.update(cand_request_ids)
                pass_reserved_student_ids.update(cand_student_ids)
                pass_reserved_bed_ids.update(cand_bed_ids)

        except (ValidationError, IntegrityError) as exc:
            logger.info("Candidate %s could not be activated: %s", candidate.cycle_signature, exc)
            continue

    return activated_proposals


def schedule_institution_matching(
    *,
    institution_id,
    trigger_reason: str = "manual",
    trigger_request_id = None,
    trigger_proposal_id = None,
) -> MatchingOutbox:
    """
    Schedule institution-scoped background matching.
    Persists durable outbox record to guarantee resilience against broker failure.
    Dispatches Celery background worker task upon transaction commit.
    """
    with transaction.atomic():
        outbox = MatchingOutbox.objects.create(
            institution_id=institution_id,
            event_type=trigger_reason,
            request_id=trigger_request_id,
            proposal_id=trigger_proposal_id,
        )

    def _dispatch_celery():
        try:
            from .tasks import run_institution_matching_task
            run_institution_matching_task.delay(
                institution_id=institution_id,
                trigger_reason=trigger_reason,
                trigger_request_id=str(trigger_request_id) if trigger_request_id else None,
            )
        except Exception as exc:
            logger.warning(
                "Celery dispatch failed for institution %s (event %s): %s. "
                "Event preserved in MatchingOutbox for periodic reconciliation.",
                institution_id,
                outbox.id,
                exc,
            )

    transaction.on_commit(_dispatch_celery)
    return outbox


def run_institution_matching(
    institution_id,
    trigger_reason: str = "manual",
    trigger_request_id = None,
    now=None,
    status_adapter=None,
) -> MatchingJob:
    """
    Execute institution-scoped matching pass in background worker.
    Coalesces rapid events: if an active job is already RUNNING for this institution,
    sets needs_rerun=True so the worker performs another pass upon completion.
    """
    from .matching import find_candidate_matches

    now = now or timezone.now()
    institution = Institution.objects.get(id=institution_id)

    with transaction.atomic():
        running_job = (
            MatchingJob.objects.select_for_update()
            .filter(institution=institution, status=MatchingJob.Status.RUNNING)
            .first()
        )
        if running_job:
            running_job.needs_rerun = True
            running_job.save(update_fields=["needs_rerun", "updated_at"])
            logger.info("Matching already running for institution %s; flagged needs_rerun.", institution.code)
            return running_job

        tr_req = None
        if trigger_request_id:
            tr_req = ExchangeRequest.objects.filter(id=trigger_request_id).first()

        job = MatchingJob.objects.create(
            institution=institution,
            status=MatchingJob.Status.RUNNING,
            trigger_reason=trigger_reason,
            trigger_request=tr_req,
            started_at=now,
        )

    # Mark open exchange requests as PROCESSING
    ExchangeRequest.objects.filter(
        institution=institution,
        status=ExchangeRequest.Status.OPEN,
        request_type=ExchangeRequest.RequestType.EXCHANGE,
    ).update(matching_status=ExchangeRequest.MatchingStatus.PROCESSING)

    total_candidates_found = 0
    total_proposals_activated = 0

    try:
        while True:
            pass_now = timezone.now()
            result = find_candidate_matches(
                institution=institution,
                now=pass_now,
                status_adapter=status_adapter,
                persist_candidates=False,
            )
            total_candidates_found += len(result.candidates)

            all_open_reqs = list(
                ExchangeRequest.objects.filter(
                    institution=institution,
                    status=ExchangeRequest.Status.OPEN,
                    request_type=ExchangeRequest.RequestType.EXCHANGE,
                ).select_related(
                    "student",
                    "current_occupancy__bed__room__floor__block__hostel",
                    "preferred_bed__room__floor__block__hostel",
                    "preferred_hostel",
                    "preferred_block",
                    "preferred_floor",
                )
            )
            requests_by_id = {str(r.id): r for r in all_open_reqs}

            unsuppressed_candidates = [
                cand for cand in result.candidates
                if not is_candidate_suppressed(cand, institution)
            ]

            activated = select_and_activate_candidates(
                candidates=unsuppressed_candidates,
                institution=institution,
                request_map=requests_by_id,
                now=pass_now,
                status_adapter=status_adapter,
            )
            total_proposals_activated += len(activated)

            activated_req_ids = set()
            for prop in activated:
                for p in prop.participants.all():
                    activated_req_ids.add(p.exchange_request_id)

            unmatched_req_ids = [r.id for r in all_open_reqs if r.id not in activated_req_ids]
            if unmatched_req_ids:
                ExchangeRequest.objects.filter(
                    id__in=unmatched_req_ids,
                    status=ExchangeRequest.Status.OPEN,
                ).update(
                    matching_status=ExchangeRequest.MatchingStatus.NO_MATCH,
                    last_matched_at=pass_now,
                )

            MatchingOutbox.objects.filter(
                institution=institution,
                processed=False,
            ).update(processed=True, processed_at=pass_now)

            with transaction.atomic():
                locked_job = MatchingJob.objects.select_for_update().get(id=job.id)
                if locked_job.needs_rerun:
                    locked_job.needs_rerun = False
                    locked_job.save(update_fields=["needs_rerun", "updated_at"])
                    continue
                else:
                    locked_job.status = MatchingJob.Status.COMPLETED
                    locked_job.completed_at = timezone.now()
                    locked_job.candidates_found_count = total_candidates_found
                    locked_job.proposals_activated_count = total_proposals_activated
                    locked_job.limit_reached = result.metrics.limit_reached
                    locked_job.save(
                        update_fields=[
                            "status",
                            "completed_at",
                            "candidates_found_count",
                            "proposals_activated_count",
                            "limit_reached",
                            "updated_at",
                        ]
                    )
                    return locked_job

    except Exception as exc:
        logger.exception("Matching failed for institution %s: %s", institution.code, exc)
        with transaction.atomic():
            job.refresh_from_db()
            job.status = MatchingJob.Status.FAILED
            job.error_message = str(exc)
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "error_message", "completed_at", "updated_at"])

            ExchangeRequest.objects.filter(
                institution=institution,
                status=ExchangeRequest.Status.OPEN,
                request_type=ExchangeRequest.RequestType.EXCHANGE,
            ).update(
                matching_status=ExchangeRequest.MatchingStatus.UNAVAILABLE,
                matching_failure_reason=str(exc)[:500],
            )
        raise


def reconcile_all_institutions_matching(now=None) -> list[int]:
    """
    Periodic sweep across all institutions:
    1. Clean up expired requests and expired proposals.
    2. Check for unprocessed MatchingOutbox entries or open requests waiting for matching.
    3. Triggers matching runs for institutions requiring reconciliation.
    """
    now = now or timezone.now()

    expire_exchange_requests(now=now)
    expire_exchange_proposals(now=now)

    institutions_to_run = set()

    unprocessed_insts = (
        MatchingOutbox.objects.filter(processed=False)
        .values_list("institution_id", flat=True)
        .distinct()
    )
    institutions_to_run.update(unprocessed_insts)

    stale_cutoff = now - timedelta(minutes=15)
    stale_insts = (
        ExchangeRequest.objects.filter(
            status=ExchangeRequest.Status.OPEN,
            request_type=ExchangeRequest.RequestType.EXCHANGE,
        )
        .filter(
            Q(matching_status=ExchangeRequest.MatchingStatus.WAITING)
            | Q(last_matched_at__isnull=True)
            | Q(last_matched_at__lt=stale_cutoff)
        )
        .values_list("institution_id", flat=True)
        .distinct()
    )
    institutions_to_run.update(stale_insts)

    for inst_id in institutions_to_run:
        schedule_institution_matching(
            institution_id=inst_id,
            trigger_reason="reconciliation_sweep",
        )

    return list(institutions_to_run)


