from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.models import AuditLog
from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
)

from .eligibility import check_student_exchange_eligibility
from .forms import ExchangePolicySettingsForm, ExchangeRequestForm
from .models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    Notification,
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)
from .selectors import (
    can_user_decide_proposal,
    can_user_decide_vacant_transfer,
    get_approval_proposal_detail,
    get_approvals_queryset,
    get_eligible_blocks_for_hostel,
    get_eligible_floors_for_block,
    get_eligible_hostels_for_exchange,
    get_floor_room_summaries_for_exchange,
    get_pending_approvals_count,
    get_pending_vacant_transfers_count,
    get_room_bed_details_for_exchange,
    get_student_exchange_requests,
    get_student_proposal,
    get_student_proposals,
    get_student_target_beds,
    get_student_target_rooms,
    get_vacant_transfer_detail,
    get_vacant_transfers_queryset,
)
from .services import (
    approve_and_execute_vacant_transfer,
    create_exchange_request,
    decide_exchange_proposal,
    expire_single_proposal,
    get_institution_exchange_policies,
    reject_vacant_transfer,
    respond_to_proposal,
    update_exchange_request,
    update_institution_exchange_policy,
    withdraw_exchange_request,
)


def _require_student(request):
    if not request.user.is_student_role:
        raise PermissionDenied("Only students can manage exchange requests.")


def _validation_messages(error):
    if hasattr(error, "error_list"):
        return [
            message.message if hasattr(message, "message") else str(message)
            for message in error.error_list
        ]
    return [str(error)]


def _bed_browser_context(request, student, form, instance=None):
    """
    Build the database-backed hostel/block/floor/room browser state.
    Lightweight, scoped to student's institution and eligibility.
    Avoids full eligible-bed-ID materialization in memory.
    """
    current_occupancy = (
        StudentOccupancy.objects.filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .select_related("bed__room__floor__block__hostel")
        .first()
    )

    selected_bed = None
    selected_bed_id = (
        request.POST.get("preferred_bed")
        or request.GET.get("bed_id")
        or (getattr(instance, "preferred_bed_id", None) if instance else None)
        or getattr(form.instance, "preferred_bed_id", None)
    )
    if selected_bed_id:
        selected_bed = (
            Bed.objects.filter(
                id=selected_bed_id,
                room__floor__block__hostel__institution=student.institution,
                room__floor__block__hostel__status=Hostel.Status.ACTIVE,
                room__floor__block__hostel__resident_category=student.residential_eligibility,
            )
            .select_related("room__floor__block__hostel")
            .first()
        )

    hostels = get_eligible_hostels_for_exchange(student)
    selected_hostel_id = (
        request.GET.get("hostel_id")
        or (selected_bed.room.floor.block.hostel_id if selected_bed else None)
        or (getattr(instance, "preferred_hostel_id", None) if instance else None)
    )
    selected_hostel = hostels.filter(id=selected_hostel_id).first() if selected_hostel_id else None
    if not selected_hostel and hostels.exists():
        selected_hostel = hostels.first()

    blocks = (
        get_eligible_blocks_for_hostel(student, selected_hostel.id)
        if selected_hostel
        else Block.objects.none()
    )
    selected_block_id = (
        request.GET.get("block_id")
        or (selected_bed.room.floor.block_id if selected_bed else None)
        or (getattr(instance, "preferred_block_id", None) if instance else None)
    )
    selected_block = blocks.filter(id=selected_block_id).first() if selected_block_id else None
    if not selected_block and blocks.exists():
        selected_block = blocks.first()

    floors = (
        get_eligible_floors_for_block(student, selected_block.id)
        if selected_block
        else Floor.objects.none()
    )
    selected_floor_id = (
        request.GET.get("floor_id")
        or (selected_bed.room.floor_id if selected_bed else None)
        or (getattr(instance, "preferred_floor_id", None) if instance else None)
    )
    selected_floor = floors.filter(id=selected_floor_id).first() if selected_floor_id else None
    if not selected_floor and floors.exists():
        selected_floor = floors.first()

    room_summaries = []
    if selected_floor:
        room_summaries = get_floor_room_summaries_for_exchange(student, selected_floor.id)

    selected_room_id = request.GET.get("room_id")
    if not selected_room_id and selected_bed:
        selected_room_id = selected_bed.room_id
    elif not selected_room_id and len(room_summaries) == 1:
        selected_room_id = room_summaries[0]["id"]

    initial_room_details = None
    if selected_room_id:
        initial_room_details = get_room_bed_details_for_exchange(student, selected_room_id)

    initial_mode = "specific_bed"
    if instance and not instance.preferred_bed and (
        instance.preferred_hostel or instance.preferred_room_type or instance.preferred_occupancy_category
    ):
        initial_mode = "preferences"

    browser_rooms = []
    for r in room_summaries:
        room_obj = Room.objects.filter(id=r["id"]).select_related("floor__block__hostel").first()
        room_detail = get_room_bed_details_for_exchange(student, r["id"]) or {}
        beds_data = []
        for b in room_detail.get("beds", []):
            bed_obj = Bed.objects.filter(id=b["id"]).first()
            beds_data.append({
                "bed": bed_obj,
                "occupied": b["is_occupied"],
                "unavailable": not b["selectable"] and not b["is_current"],
                "selectable": b["selectable"],
                "selected": selected_bed and str(b["id"]) == str(selected_bed.id),
                "current": b["is_current"],
            })
        browser_rooms.append({"room": room_obj, "beds": beds_data})

    return {
        "current_occupancy": current_occupancy,
        "browser_hostels": hostels,
        "browser_blocks": blocks,
        "browser_floors": floors,
        "room_summaries": room_summaries,
        "selected_hostel": selected_hostel,
        "selected_block": selected_block,
        "selected_floor": selected_floor,
        "selected_room_id": str(selected_room_id) if selected_room_id else "",
        "selected_bed": selected_bed,
        "selected_bed_id": str(selected_bed.id) if selected_bed else "",
        "initial_room_details": initial_room_details,
        "initial_mode": initial_mode,
        "browser_rooms": browser_rooms,
    }


# =========================================================================
# Lightweight AJAX Endpoints for Dynamic Exchange Browser
# =========================================================================

@login_required
@require_GET
def api_exchange_blocks(request, hostel_id):
    _require_student(request)
    blocks = get_eligible_blocks_for_hostel(request.user, hostel_id)
    return JsonResponse({
        "blocks": [{"id": str(b.id), "name": b.name, "code": b.code} for b in blocks]
    })


@login_required
@require_GET
def api_exchange_floors(request, block_id):
    _require_student(request)
    floors = get_eligible_floors_for_block(request.user, block_id)
    return JsonResponse({
        "floors": [
            {
                "id": str(f.id),
                "floor_number": f.floor_number,
                "floor_label": f.floor_label,
            }
            for f in floors
        ]
    })


@login_required
@require_GET
def api_exchange_floor_rooms(request, floor_id):
    _require_student(request)
    rooms = get_floor_room_summaries_for_exchange(request.user, floor_id)
    return JsonResponse({"rooms": rooms})


@login_required
@require_GET
def api_exchange_room_beds(request, room_id):
    _require_student(request)
    data = get_room_bed_details_for_exchange(request.user, room_id)
    if not data:
        raise Http404("Room not found or not eligible for exchange.")
    return JsonResponse(data)


# =========================================================================
# Views
# =========================================================================

@login_required
def exchange_request_list_view(request):
    _require_student(request)
    return render(
        request,
        "exchange/request_list.html",
        {
            "exchange_requests": get_student_exchange_requests(request.user),
            "active_nav": "exchange",
        },
    )


@login_required
def exchange_notifications_view(request):
    _require_student(request)
    return render(
        request,
        "exchange/notifications.html",
        {
            "notifications": Notification.objects.filter(
                recipient=request.user,
            )
            .select_related("exchange_request", "proposal")
            .order_by("-created_at"),
            "active_nav": "notifications",
        },
    )


@login_required
def exchange_request_create_view(request):
    _require_student(request)
    if request.method == "POST":
        form = ExchangeRequestForm(request.POST, student=request.user)
        if form.is_valid():
            try:
                create_exchange_request(
                    student=request.user,
                    data=form.cleaned_data,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, "Your exchange request was created.")
                return redirect("exchange:request_list")
            except ValidationError as error:
                for message in _validation_messages(error):
                    form.add_error(None, message)
    else:
        form = ExchangeRequestForm(student=request.user)
    browser_context = _bed_browser_context(request, request.user, form)
    eligibility_report = check_student_exchange_eligibility(
        student=request.user,
        occupancy=browser_context.get("current_occupancy"),
    )
    return render(
        request,
        "exchange/request_form.html",
        {
            "form": form,
            "page_title": "Create Exchange Request",
            "active_nav": "exchange",
            "eligibility_report": eligibility_report,
            **browser_context,
        },
    )


@login_required
def exchange_request_detail_view(request, pk):
    _require_student(request)
    exchange_request = get_object_or_404(
        get_student_exchange_requests(request.user),
        pk=pk,
    )
    now = timezone.now()
    is_expired = bool(exchange_request.expires_at and exchange_request.expires_at <= now)
    has_active_proposal = exchange_request.proposal_participations.filter(is_active_reservation=True).exists()
    can_edit = exchange_request.status == ExchangeRequest.Status.OPEN and not is_expired and not has_active_proposal
    can_withdraw = (
        exchange_request.status in (ExchangeRequest.Status.OPEN, ExchangeRequest.Status.PENDING_APPROVAL)
        and not is_expired
        and not has_active_proposal
    )

    # Section 9 check: open exchange request whose target bed happens to be vacant
    is_target_bed_vacant = False
    if (
        exchange_request.request_type == ExchangeRequest.RequestType.EXCHANGE
        and exchange_request.status == ExchangeRequest.Status.OPEN
        and exchange_request.preferred_bed
        and exchange_request.preferred_bed.is_active
        and exchange_request.preferred_bed.operational_status == OperationalStatus.IN_SERVICE
    ):
        is_target_bed_vacant = not StudentOccupancy.objects.filter(
            bed=exchange_request.preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists()

    # Durable transfer record if completed
    transfer_record = None
    if exchange_request.status == ExchangeRequest.Status.COMPLETED:
        transfer_record = TransferRecord.objects.filter(exchange_request=exchange_request).first()

    latest_approval = exchange_request.approvals.order_by("-decided_at", "-created_at").first()

    required_authority_label = "Assigned Hostel Warden"
    if exchange_request.preferred_bed:
        source_h = exchange_request.current_occupancy.bed.room.floor.block.hostel
        dest_h = exchange_request.preferred_bed.room.floor.block.hostel
        if source_h.id != dest_h.id:
            required_authority_label = "Chief Warden (Cross-Hostel)"

    return render(
        request,
        "exchange/request_detail.html",
        {
            "exchange_request": exchange_request,
            "can_modify": can_edit,
            "can_edit": can_edit,
            "can_withdraw": can_withdraw,
            "is_expired": is_expired,
            "has_active_proposal": has_active_proposal,
            "is_target_bed_vacant": is_target_bed_vacant,
            "transfer_record": transfer_record,
            "latest_approval": latest_approval,
            "required_authority_label": required_authority_label,
            "active_nav": "exchange",
        },
    )


@login_required
def exchange_request_edit_view(request, pk):
    _require_student(request)
    exchange_request = get_object_or_404(
        get_student_exchange_requests(request.user),
        pk=pk,
    )
    if request.method == "POST":
        form = ExchangeRequestForm(
            request.POST,
            instance=exchange_request,
            student=request.user,
        )
        if form.is_valid():
            try:
                update_exchange_request(
                    exchange_request=exchange_request,
                    student=request.user,
                    data=form.cleaned_data,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, "Your exchange request was updated.")
                return redirect("exchange:request_detail", pk=exchange_request.pk)
            except ValidationError as error:
                for message in _validation_messages(error):
                    form.add_error(None, message)
    else:
        form = ExchangeRequestForm(
            instance=exchange_request,
            student=request.user,
        )
    browser_context = _bed_browser_context(
        request, request.user, form, instance=exchange_request
    )
    eligibility_report = check_student_exchange_eligibility(
        student=request.user,
        occupancy=browser_context.get("current_occupancy"),
    )
    return render(
        request,
        "exchange/request_form.html",
        {
            "form": form,
            "exchange_request": exchange_request,
            "page_title": "Edit Exchange Request",
            "active_nav": "exchange",
            "eligibility_report": eligibility_report,
            **browser_context,
        },
    )


@login_required
@require_POST
def exchange_request_withdraw_view(request, pk):
    _require_student(request)
    exchange_request = get_object_or_404(
        get_student_exchange_requests(request.user),
        pk=pk,
    )
    try:
        withdraw_exchange_request(
            exchange_request=exchange_request,
            student=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Your exchange request was withdrawn.")
    except ValidationError as error:
        messages.error(request, "; ".join(_validation_messages(error)))
    return redirect("exchange:request_detail", pk=pk)


# =========================================================================
# Multi-Party Exchange Proposal Views (Student Facing)
# =========================================================================

@login_required
def proposal_list_view(request):
    _require_student(request)
    now = timezone.now()
    proposals = get_student_proposals(request.user)

    proposal_cards = []
    for proposal in proposals:
        if (
            proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
            and proposal.expires_at
            and proposal.expires_at <= now
        ):
            expire_single_proposal(proposal, now=now)
            proposal.refresh_from_db()

        my_part = next((p for p in proposal.participants.all() if p.student_id == request.user.id), None)
        accepted_count = sum(
            1 for p in proposal.participants.all()
            if p.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
        )
        total_count = proposal.cycle_length
        is_expired = proposal.status == ExchangeProposal.Status.EXPIRED or bool(
            proposal.expires_at and proposal.expires_at <= now
        )
        can_respond = (
            proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
            and my_part is not None
            and my_part.response_status == ProposalParticipant.ResponseStatus.PENDING
            and not is_expired
        )
        proposal_cards.append({
            "proposal": proposal,
            "my_participation": my_part,
            "accepted_count": accepted_count,
            "total_count": total_count,
            "is_expired": is_expired,
            "can_respond": can_respond,
            "cycle_label": "Direct Swap (2 students)" if total_count == 2 else f"{total_count}-Party Rotation Cycle",
        })

    return render(
        request,
        "exchange/proposal_list.html",
        {
            "proposal_cards": proposal_cards,
            "active_nav": "proposals",
        },
    )


@login_required
def proposal_detail_view(request, pk):
    _require_student(request)
    now = timezone.now()
    proposal = get_student_proposal(request.user, pk)
    if not proposal:
        raise Http404("Proposal not found.")

    if (
        proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
        and proposal.expires_at
        and proposal.expires_at <= now
    ):
        expire_single_proposal(proposal, now=now)
        proposal = get_student_proposal(request.user, pk)

    my_part = next((p for p in proposal.participants.all() if p.student_id == request.user.id), None)
    if not my_part:
        raise PermissionDenied("You are not an authorized participant in this exchange proposal.")

    participants = list(proposal.participants.all().order_by("sequence_order"))
    accepted_count = sum(
        1 for p in participants
        if p.response_status == ProposalParticipant.ResponseStatus.ACCEPTED
    )
    total_count = proposal.cycle_length
    is_expired = proposal.status == ExchangeProposal.Status.EXPIRED or bool(
        proposal.expires_at and proposal.expires_at <= now
    )
    can_respond = (
        proposal.status == ExchangeProposal.Status.PENDING_ACCEPTANCE
        and my_part.response_status == ProposalParticipant.ResponseStatus.PENDING
        and not is_expired
    )

    # Build cycle rotation cards with strict zero PII leakage:
    # No student email, phone number, or disciplinary/fee hold information.
    cycle_steps = []
    for p in participants:
        is_me = p.student_id == request.user.id
        label = "You (Your Request)" if is_me else f"Participant {p.sequence_order + 1}"
        cycle_steps.append({
            "is_me": is_me,
            "label": label,
            "response_status": p.response_status,
            "response_status_display": p.get_response_status_display(),
            "responded_at": p.responded_at,
            "current_hostel": p.current_occupancy.bed.room.floor.block.hostel.name,
            "current_room": p.current_occupancy.bed.room.room_number,
            "current_bed": p.current_occupancy.bed.bed_label,
            "proposed_hostel": p.proposed_bed.room.floor.block.hostel.name,
            "proposed_room": p.proposed_bed.room.room_number,
            "proposed_bed": p.proposed_bed.bed_label,
        })

    progress_percent = int((accepted_count / total_count) * 100) if total_count > 0 else 0

    return render(
        request,
        "exchange/proposal_detail.html",
        {
            "proposal": proposal,
            "my_participation": my_part,
            "cycle_steps": cycle_steps,
            "accepted_count": accepted_count,
            "total_count": total_count,
            "progress_percent": progress_percent,
            "is_expired": is_expired,
            "can_respond": can_respond,
            "cycle_label": "Direct Swap (2 students)" if total_count == 2 else f"{total_count}-Party Rotation Cycle",
            "active_nav": "proposals",
        },
    )


@login_required
@require_POST
def proposal_respond_view(request, pk):
    _require_student(request)
    action = request.POST.get("action", "").strip().lower()
    if action not in ("accept", "decline"):
        messages.error(request, "Invalid response action.")
        return redirect("exchange:proposal_detail", pk=pk)

    try:
        participant, proposal = respond_to_proposal(
            proposal_id=pk,
            student=request.user,
            action=action,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        if action == "accept":
            messages.success(request, "You have accepted the room exchange proposal.")
        else:
            messages.info(request, "You have declined the room exchange proposal. The proposal has been cancelled.")
    except (ValidationError, PermissionDenied) as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)

    return redirect("exchange:proposal_detail", pk=pk)



@login_required
def exchange_policy_settings_view(request):
    """Hostel Administrator policy configuration view."""
    if not getattr(request.user, "is_hostel_admin_role", False):
        raise PermissionDenied("Only Hostel Administrators can manage exchange policy rules.")

    institution = getattr(request.user, "institution", None)
    if not institution:
        raise PermissionDenied("Your account is not associated with an institution.")

    if request.method == "POST":
        form = ExchangePolicySettingsForm(request.POST)
        if form.is_valid():
            ip = request.META.get("REMOTE_ADDR")
            cd = form.cleaned_data
            try:
                update_institution_exchange_policy(
                    acting_user=request.user,
                    institution=institution,
                    key=PolicyRule.Key.COOLING_OFF_DAYS,
                    value=cd["cooling_off_days"],
                    ip_address=ip,
                )
                update_institution_exchange_policy(
                    acting_user=request.user,
                    institution=institution,
                    key=PolicyRule.Key.REQUEST_VALIDITY_DAYS,
                    value=cd["request_validity_days"],
                    ip_address=ip,
                )
                if cd.get("proposal_acceptance_window_hours") is not None:
                    update_institution_exchange_policy(
                        acting_user=request.user,
                        institution=institution,
                        key=PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS,
                        value=cd["proposal_acceptance_window_hours"],
                        ip_address=ip,
                    )
                update_institution_exchange_policy(
                    acting_user=request.user,
                    institution=institution,
                    key=PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
                    value=cd["require_fee_clearance"],
                    ip_address=ip,
                )
                update_institution_exchange_policy(
                    acting_user=request.user,
                    institution=institution,
                    key=PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD,
                    value=cd["require_no_disciplinary_hold"],
                    ip_address=ip,
                )
                messages.success(request, "Exchange policies have been successfully updated.")
                return redirect("exchange:policy_settings")
            except ValidationError as error:
                for msg in _validation_messages(error):
                    form.add_error(None, msg)
    else:
        policies = get_institution_exchange_policies(institution)
        initial_data = {}
        if policies.get(PolicyRule.Key.COOLING_OFF_DAYS) is not None:
            initial_data["cooling_off_days"] = policies[PolicyRule.Key.COOLING_OFF_DAYS]
        if policies.get(PolicyRule.Key.REQUEST_VALIDITY_DAYS) is not None:
            initial_data["request_validity_days"] = policies[PolicyRule.Key.REQUEST_VALIDITY_DAYS]
        if policies.get(PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS) is not None:
            initial_data["proposal_acceptance_window_hours"] = policies[PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS]
        if policies.get(PolicyRule.Key.REQUIRE_FEE_CLEARANCE) is not None:
            initial_data["require_fee_clearance"] = policies[PolicyRule.Key.REQUIRE_FEE_CLEARANCE]
        if policies.get(PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD) is not None:
            initial_data["require_no_disciplinary_hold"] = policies[PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD]

        form = ExchangePolicySettingsForm(initial=initial_data)

    recent_audits = (
        AuditLog.objects.filter(
            institution=institution,
            target_type="PolicyRule",
        )
        .select_related("acting_user")
        .order_by("-timestamp")[:10]
    )

    policies = get_institution_exchange_policies(institution)
    all_configured = all(v is not None for v in policies.values())

    return render(
        request,
        "exchange/policy_settings.html",
        {
            "form": form,
            "recent_audits": recent_audits,
            "all_configured": all_configured,
            "active_nav": "exchange_policies",
            "page_title": "Exchange Policy Settings",
        },
    )


# =========================================================================
# Module M5: Exchange Approval Workflow Views (Warden, Chief Warden, Dean)
# =========================================================================

@login_required
def exchange_approval_list_view(request):
    """Authority approval queue for exchange proposals and vacant-bed transfer requests."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)
    is_dean = getattr(user, "is_dean_role", False)

    if not (is_w or is_cw or is_dean):
        raise PermissionDenied("You do not have permission to access exchange approvals.")

    queue_filter = request.GET.get("queue", "all").strip().lower()
    status_filter = request.GET.get("status", "pending_approval").strip().lower()
    search_query = request.GET.get("q", "").strip()
    hostel_filter = request.GET.get("hostel", "").strip()

    proposals_base = get_approvals_queryset(user)
    transfers_base = get_vacant_transfers_queryset(user)

    # Metrics calculation
    prop_pending = proposals_base.filter(status=ExchangeProposal.Status.PENDING_APPROVAL).count()
    prop_approved = proposals_base.filter(status=ExchangeProposal.Status.APPROVED).count()
    prop_rejected = proposals_base.filter(status=ExchangeProposal.Status.REJECTED).count()
    prop_total = proposals_base.count()

    trans_pending = transfers_base.filter(status=ExchangeRequest.Status.PENDING_APPROVAL).count()
    trans_approved = transfers_base.filter(status=ExchangeRequest.Status.COMPLETED).count()
    trans_rejected = transfers_base.filter(status=ExchangeRequest.Status.REJECTED).count()
    trans_total = transfers_base.count()

    if queue_filter == "transfers":
        pending_count = trans_pending
        approved_count = trans_approved
        rejected_count = trans_rejected
        total_count = trans_total
    elif queue_filter == "exchanges":
        pending_count = prop_pending
        approved_count = prop_approved
        rejected_count = prop_rejected
        total_count = prop_total
    else:
        pending_count = prop_pending + trans_pending
        approved_count = prop_approved + trans_approved
        rejected_count = prop_rejected + trans_rejected
        total_count = prop_total + trans_total

    # Proposals filtering
    prop_qs = proposals_base
    if status_filter and status_filter != "all":
        valid_statuses = {
            "pending_approval": ExchangeProposal.Status.PENDING_APPROVAL,
            "approved": ExchangeProposal.Status.APPROVED,
            "rejected": ExchangeProposal.Status.REJECTED,
        }
        if status_filter in valid_statuses:
            prop_qs = prop_qs.filter(status=valid_statuses[status_filter])
        else:
            prop_qs = prop_qs.none()
    if search_query:
        prop_qs = prop_qs.filter(
            Q(id__icontains=search_query)
            | Q(participants__student__full_name__icontains=search_query)
            | Q(participants__student__email__icontains=search_query)
            | Q(participants__student__registration_number__icontains=search_query)
        ).distinct()
    if hostel_filter:
        try:
            h_id = int(hostel_filter)
            prop_qs = prop_qs.filter(
                participants__current_occupancy__bed__room__floor__block__hostel_id=h_id
            ).distinct()
        except (ValueError, TypeError):
            pass

    # Transfers filtering
    trans_qs = transfers_base
    if status_filter and status_filter != "all":
        valid_t_statuses = {
            "pending_approval": ExchangeRequest.Status.PENDING_APPROVAL,
            "approved": ExchangeRequest.Status.COMPLETED,
            "rejected": ExchangeRequest.Status.REJECTED,
        }
        if status_filter in valid_t_statuses:
            trans_qs = trans_qs.filter(status=valid_t_statuses[status_filter])
        else:
            trans_qs = trans_qs.none()
    if search_query:
        trans_qs = trans_qs.filter(
            Q(id__icontains=search_query)
            | Q(student__full_name__icontains=search_query)
            | Q(student__email__icontains=search_query)
            | Q(student__registration_number__icontains=search_query)
            | Q(reason__icontains=search_query)
        ).distinct()
    if hostel_filter:
        try:
            h_id = int(hostel_filter)
            trans_qs = trans_qs.filter(
                Q(current_occupancy__bed__room__floor__block__hostel_id=h_id)
                | Q(preferred_bed__room__floor__block__hostel_id=h_id)
            ).distinct()
        except (ValueError, TypeError):
            pass

    items = []
    if queue_filter in ("all", "exchanges"):
        for p in prop_qs:
            can_decide, _ = can_user_decide_proposal(user, p)
            items.append({
                "type": "proposal",
                "obj": p,
                "id": str(p.id),
                "created_at": p.created_at,
                "is_vacant_transfer": False,
                "is_cross_hostel": p.is_cross_hostel,
                "scope_hostel": p.scope_hostel,
                "status": p.status,
                "status_display": p.get_status_display(),
                "cycle_length": p.cycle_length,
                "participants": list(p.participants.all()),
                "can_decide": can_decide,
            })

    if queue_filter in ("all", "transfers"):
        for t in trans_qs:
            src_hostel = t.current_occupancy.bed.room.floor.block.hostel
            dest_hostel = t.preferred_bed.room.floor.block.hostel if t.preferred_bed else None
            is_cross = dest_hostel and (src_hostel.id != dest_hostel.id)
            can_decide, _ = can_user_decide_vacant_transfer(user, t)
            items.append({
                "type": "transfer",
                "obj": t,
                "id": str(t.id),
                "created_at": t.submitted_at or t.created_at,
                "is_vacant_transfer": True,
                "is_cross_hostel": is_cross,
                "scope_hostel": src_hostel,
                "status": t.status,
                "status_display": "Approved (Transfer Executed)" if t.status == ExchangeRequest.Status.COMPLETED else t.get_status_display(),
                "cycle_length": 1,
                "student": t.student,
                "source_hostel": src_hostel,
                "source_bed": t.current_occupancy.bed,
                "dest_hostel": dest_hostel,
                "dest_bed": t.preferred_bed,
                "can_decide": can_decide,
            })

    items.sort(key=lambda x: x["created_at"], reverse=True)

    paginator = Paginator(items, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    filter_hostels = Hostel.objects.none()
    if (is_dean or is_cw) and getattr(user, "institution", None):
        filter_hostels = Hostel.objects.filter(institution=user.institution, status=Hostel.Status.ACTIVE).order_by("name")

    context = {
        "page_obj": page_obj,
        "queue_filter": queue_filter,
        "status_filter": status_filter,
        "search_query": search_query,
        "hostel_filter": hostel_filter,
        "pending_count": pending_count,
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "total_count": total_count,
        "prop_pending": prop_pending,
        "trans_pending": trans_pending,
        "filter_hostels": filter_hostels,
        "is_dean": is_dean,
        "is_chief_warden": is_cw,
        "is_warden": is_w,
        "active_nav": "exchange_approvals",
    }
    return render(request, "exchange/approval_list.html", context)


@login_required
def exchange_approval_detail_view(request, pk):
    """Detailed view for reviewing a proposal awaiting approval or decided."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)
    is_dean = getattr(user, "is_dean_role", False)

    if not (is_w or is_cw or is_dean):
        raise PermissionDenied("You do not have permission to access exchange approvals.")

    proposal = get_approval_proposal_detail(user, pk)
    if not proposal:
        raise Http404("Proposal not found or access restricted.")

    can_decide, cant_decide_reason = can_user_decide_proposal(user, proposal)

    participants = list(
        proposal.participants.select_related(
            "student",
            "current_occupancy__bed__room__floor__block__hostel",
            "proposed_bed__room__floor__block__hostel",
            "exchange_request",
        ).order_by("sequence_order")
    )

    # Build rotation cycle display steps
    cycle_steps = []
    total_p = len(participants)
    for i, p in enumerate(participants):
        next_p = participants[(i + 1) % total_p]
        cycle_steps.append({
            "participant": p,
            "from_occupancy": p.current_occupancy,
            "to_bed": p.proposed_bed,
            "destination_student": next_p.student,
        })

    # Approvals history
    approvals = list(
        proposal.approvals.select_related("authority_user", "scope_hostel").order_by("-decided_at", "-created_at")
    )
    latest_approval = approvals[0] if approvals else None

    # Audit history for this proposal
    audit_logs = (
        AuditLog.objects.filter(
            institution=user.institution,
            target_id=str(proposal.id),
        )
        .select_related("acting_user")
        .order_by("timestamp")
    )

    context = {
        "proposal": proposal,
        "participants": participants,
        "cycle_steps": cycle_steps,
        "can_decide": can_decide,
        "cant_decide_reason": cant_decide_reason,
        "approvals": approvals,
        "latest_approval": latest_approval,
        "audit_logs": audit_logs,
        "is_dean": is_dean,
        "is_chief_warden": is_cw,
        "is_warden": is_w,
        "active_nav": "exchange_approvals",
    }
    return render(request, "exchange/approval_detail.html", context)


@login_required
@require_POST
def exchange_approval_decide_view(request, pk):
    """Handle authority decision (Approve / Reject) with remarks."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)

    if not (is_w or is_cw):
        raise PermissionDenied("You do not have authority to decide exchange proposals.")

    decision = request.POST.get("decision", "").strip().lower()
    remarks = request.POST.get("remarks", "").strip()

    if decision not in ("approved", "rejected"):
        messages.error(request, "Invalid decision value.")
        return redirect("exchange:approval_detail", pk=pk)

    if decision == "rejected" and not remarks:
        messages.error(request, "Remarks are mandatory when rejecting an exchange proposal.")
        return redirect("exchange:approval_detail", pk=pk)

    try:
        approval, proposal = decide_exchange_proposal(
            proposal_id=pk,
            actor=user,
            decision=decision,
            remarks=remarks,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        if decision == "approved":
            messages.success(
                request,
                f"Exchange proposal #{str(proposal.id)[:8]} has been approved successfully. Status updated to 'Approved — transfer pending'.",
            )
        else:
            messages.success(
                request,
                f"Exchange proposal #{str(proposal.id)[:8]} has been rejected. Participants have been notified.",
            )
    except ValidationError as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("exchange:approval_detail", pk=pk)


@login_required
def exchange_vacant_transfer_approval_detail_view(request, pk):
    """Detailed view for reviewing a vacant-bed transfer request awaiting approval or decided."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)
    is_dean = getattr(user, "is_dean_role", False)

    if not (is_w or is_cw or is_dean):
        raise PermissionDenied("You do not have permission to access transfer approvals.")

    transfer_request = get_vacant_transfer_detail(user, pk)
    if not transfer_request:
        raise Http404("Transfer request not found or access restricted.")

    can_decide, cant_decide_reason = can_user_decide_vacant_transfer(user, transfer_request)

    student = transfer_request.student
    current_occupancy = transfer_request.current_occupancy
    preferred_bed = transfer_request.preferred_bed

    # Live vacancy check of destination bed
    is_dest_vacant = False
    dest_operational = False
    if preferred_bed:
        dest_operational = (
            preferred_bed.is_active
            and preferred_bed.operational_status == OperationalStatus.IN_SERVICE
            and preferred_bed.room.is_active
            and preferred_bed.room.operational_status == OperationalStatus.IN_SERVICE
        )
        is_dest_vacant = dest_operational and not StudentOccupancy.objects.filter(
            bed=preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists()

    # Eligibility evaluation
    eligibility_report = check_student_exchange_eligibility(
        student=student,
        occupancy=current_occupancy,
    )

    # Source and destination hostels
    source_hostel = current_occupancy.bed.room.floor.block.hostel
    dest_hostel = preferred_bed.room.floor.block.hostel if preferred_bed else None
    is_cross_hostel = dest_hostel and (source_hostel.id != dest_hostel.id)

    # Approvals & Audit logs
    approvals = list(
        transfer_request.approvals.select_related("authority_user", "scope_hostel").order_by("-decided_at", "-created_at")
    )
    latest_approval = approvals[0] if approvals else None

    audit_logs = (
        AuditLog.objects.filter(
            institution=user.institution,
            target_id=str(transfer_request.id),
        )
        .select_related("acting_user")
        .order_by("timestamp")
    )

    # Check for transfer record if completed
    transfer_record = TransferRecord.objects.filter(exchange_request=transfer_request).first()

    context = {
        "transfer_request": transfer_request,
        "student": student,
        "current_occupancy": current_occupancy,
        "preferred_bed": preferred_bed,
        "source_hostel": source_hostel,
        "dest_hostel": dest_hostel,
        "is_cross_hostel": is_cross_hostel,
        "is_dest_vacant": is_dest_vacant,
        "dest_operational": dest_operational,
        "eligibility_report": eligibility_report,
        "can_decide": can_decide,
        "cant_decide_reason": cant_decide_reason,
        "approvals": approvals,
        "latest_approval": latest_approval,
        "transfer_record": transfer_record,
        "audit_logs": audit_logs,
        "is_dean": is_dean,
        "is_chief_warden": is_cw,
        "is_warden": is_w,
        "active_nav": "exchange_approvals",
    }
    return render(request, "exchange/transfer_approval_detail.html", context)


@login_required
@require_POST
def exchange_vacant_transfer_approval_decide_view(request, pk):
    """Handle authority decision (Approve & Execute / Reject) on a vacant-bed transfer."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)

    if not (is_w or is_cw):
        raise PermissionDenied("You do not have authority to decide transfer requests.")

    decision = request.POST.get("decision", "").strip().lower()
    remarks = request.POST.get("remarks", "").strip()

    if decision not in ("approved", "rejected"):
        messages.error(request, "Invalid decision value.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    if decision == "rejected" and not remarks:
        messages.error(request, "Remarks are mandatory when rejecting a transfer request.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    try:
        if decision == "approved":
            approval, transfer_record = approve_and_execute_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request approved and executed successfully. "
                f"Student {transfer_record.student.full_name} is now allocated to {transfer_record.destination_hostel_name}, "
                f"Room {transfer_record.destination_room_number}, Bed {transfer_record.destination_bed_label}.",
            )
        else:
            approval, req = reject_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request for {req.student.full_name} has been rejected. Student's current room allocation is unchanged.",
            )
    except ValidationError as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("exchange:transfer_approval_detail", pk=pk)


@login_required
def transfer_letter_view(request, pk):
    """
    Render or download the official RoomLoop Accommodation Transfer Letter for a completed transfer.
    Accessible by:
    - The transferred student.
    - Wardens, Chief Wardens, and Deans of the institution.
    """
    user = request.user
    transfer_record = (
        TransferRecord.objects.filter(Q(id=pk) | Q(exchange_request_id=pk))
        .select_related(
            "student",
            "institution",
            "approval__authority_user",
            "previous_room__floor__block__hostel",
            "previous_bed",
            "new_room__floor__block__hostel",
            "new_bed",
            "approved_by",
        )
        .first()
    )
    if not transfer_record:
        raise Http404("Transfer record not found.")

    is_owner = (user.id == transfer_record.student_id)
    is_staff = (
        user.institution_id == transfer_record.institution_id
        and (
            getattr(user, "is_warden_role", False)
            or getattr(user, "is_chief_warden_role", False)
            or getattr(user, "is_dean_role", False)
        )
    )

    if not (is_owner or is_staff):
        raise PermissionDenied("You do not have permission to view this transfer document.")

    download_format = request.GET.get("format") or request.GET.get("download")

    context = {
        "transfer": transfer_record,
        "student": transfer_record.student,
        "institution": transfer_record.institution,
        "approval": transfer_record.approval,
        "now": timezone.now(),
    }

    response = render(request, "exchange/transfer_letter.html", context)
    if download_format in ("1", "true", "download"):
        safe_reg = getattr(transfer_record.student, "registration_number", "student") or "student"
        response["Content-Disposition"] = f'attachment; filename="transfer_letter_{safe_reg}.html"'
    return response

    if decision == "rejected" and not remarks:
        messages.error(request, "Remarks are mandatory when rejecting an exchange proposal.")
        return redirect("exchange:approval_detail", pk=pk)


@login_required
def exchange_vacant_transfer_approval_detail_view(request, pk):
    """Detailed view for reviewing a vacant-bed transfer request awaiting approval or decided."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)
    is_dean = getattr(user, "is_dean_role", False)

    if not (is_w or is_cw or is_dean):
        raise PermissionDenied("You do not have permission to access transfer approvals.")

    transfer_request = get_vacant_transfer_detail(user, pk)
    if not transfer_request:
        raise Http404("Transfer request not found or access restricted.")

    can_decide, cant_decide_reason = can_user_decide_vacant_transfer(user, transfer_request)

    student = transfer_request.student
    current_occupancy = transfer_request.current_occupancy
    preferred_bed = transfer_request.preferred_bed

    # Live vacancy check of destination bed
    is_dest_vacant = False
    dest_operational = False
    if preferred_bed:
        dest_operational = (
            preferred_bed.is_active
            and preferred_bed.operational_status == OperationalStatus.IN_SERVICE
            and preferred_bed.room.is_active
            and preferred_bed.room.operational_status == OperationalStatus.IN_SERVICE
        )
        is_dest_vacant = dest_operational and not StudentOccupancy.objects.filter(
            bed=preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists()

    # Eligibility evaluation
    eligibility_report = check_student_exchange_eligibility(
        student=student,
        occupancy=current_occupancy,
    )

    # Source and destination hostels
    source_hostel = current_occupancy.bed.room.floor.block.hostel
    dest_hostel = preferred_bed.room.floor.block.hostel if preferred_bed else None
    is_cross_hostel = dest_hostel and (source_hostel.id != dest_hostel.id)

    # Approvals & Audit logs
    approvals = list(
        transfer_request.approvals.select_related("authority_user", "scope_hostel").order_by("-decided_at", "-created_at")
    )
    latest_approval = approvals[0] if approvals else None

    audit_logs = (
        AuditLog.objects.filter(
            institution=user.institution,
            target_id=str(transfer_request.id),
        )
        .select_related("acting_user")
        .order_by("timestamp")
    )

    # Check for transfer record if completed
    transfer_record = TransferRecord.objects.filter(exchange_request=transfer_request).first()

    context = {
        "transfer_request": transfer_request,
        "student": student,
        "current_occupancy": current_occupancy,
        "preferred_bed": preferred_bed,
        "source_hostel": source_hostel,
        "dest_hostel": dest_hostel,
        "is_cross_hostel": is_cross_hostel,
        "is_dest_vacant": is_dest_vacant,
        "dest_operational": dest_operational,
        "eligibility_report": eligibility_report,
        "can_decide": can_decide,
        "cant_decide_reason": cant_decide_reason,
        "approvals": approvals,
        "latest_approval": latest_approval,
        "transfer_record": transfer_record,
        "audit_logs": audit_logs,
        "is_dean": is_dean,
        "is_chief_warden": is_cw,
        "is_warden": is_w,
        "active_nav": "exchange_approvals",
    }
    return render(request, "exchange/transfer_approval_detail.html", context)


@login_required
@require_POST
def exchange_vacant_transfer_approval_decide_view(request, pk):
    """Handle authority decision (Approve & Execute / Reject) on a vacant-bed transfer."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)

    if not (is_w or is_cw):
        raise PermissionDenied("You do not have authority to decide transfer requests.")

    decision = request.POST.get("decision", "").strip().lower()
    remarks = request.POST.get("remarks", "").strip()

    if decision not in ("approved", "rejected"):
        messages.error(request, "Invalid decision value.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    if decision == "rejected" and not remarks:
        messages.error(request, "Remarks are mandatory when rejecting a transfer request.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    try:
        if decision == "approved":
            approval, transfer_record = approve_and_execute_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request approved and executed successfully. "
                f"Student {transfer_record.student.full_name} is now allocated to {transfer_record.destination_hostel_name}, "
                f"Room {transfer_record.destination_room_number}, Bed {transfer_record.destination_bed_label}.",
            )
        else:
            approval, req = reject_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request for {req.student.full_name} has been rejected. Student's current room allocation is unchanged.",
            )
    except ValidationError as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("exchange:transfer_approval_detail", pk=pk)


@login_required
def transfer_letter_view(request, pk):
    """
    Render or download the official RoomLoop Accommodation Transfer Letter for a completed transfer.
    Accessible by:
    - The transferred student.
    - Wardens, Chief Wardens, and Deans of the institution.
    """
    user = request.user
    transfer_record = (
        TransferRecord.objects.filter(Q(id=pk) | Q(exchange_request_id=pk))
        .select_related(
            "student",
            "institution",
            "approval__authority_user",
            "previous_room__floor__block__hostel",
            "previous_bed",
            "new_room__floor__block__hostel",
            "new_bed",
            "approved_by",
        )
        .first()
    )
    if not transfer_record:
        raise Http404("Transfer record not found.")

    is_owner = (user.id == transfer_record.student_id)
    is_staff = (
        user.institution_id == transfer_record.institution_id
        and (
            getattr(user, "is_warden_role", False)
            or getattr(user, "is_chief_warden_role", False)
            or getattr(user, "is_dean_role", False)
        )
    )

    if not (is_owner or is_staff):
        raise PermissionDenied("You do not have permission to view this transfer document.")

    download_format = request.GET.get("format") or request.GET.get("download")

    context = {
        "transfer": transfer_record,
        "student": transfer_record.student,
        "institution": transfer_record.institution,
        "approval": transfer_record.approval,
        "now": timezone.now(),
    }

    response = render(request, "exchange/transfer_letter.html", context)
    if download_format in ("1", "true", "download"):
        safe_reg = getattr(transfer_record.student, "registration_number", "student") or "student"
        response["Content-Disposition"] = f'attachment; filename="transfer_letter_{safe_reg}.html"'
    return response

    try:
        approval, proposal = decide_exchange_proposal(
            proposal_id=pk,
            actor=user,
            decision=decision,
            remarks=remarks,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        if decision == "approved":
            messages.success(
                request,
                f"Proposal #{str(proposal.id)[:8]} has been approved successfully. "
                "Workflow reservations are held for transfer execution (M6).",
            )
        else:
            messages.success(
                request,
                f"Proposal #{str(proposal.id)[:8]} has been rejected. "
                "Workflow reservations have been released and open requests returned to the matching pool.",
            )
    except ValidationError as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("exchange:approval_detail", pk=pk)


@login_required
def exchange_vacant_transfer_approval_detail_view(request, pk):
    """Detailed view for reviewing a vacant-bed transfer request awaiting approval or decided."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)
    is_dean = getattr(user, "is_dean_role", False)

    if not (is_w or is_cw or is_dean):
        raise PermissionDenied("You do not have permission to access transfer approvals.")

    transfer_request = get_vacant_transfer_detail(user, pk)
    if not transfer_request:
        raise Http404("Transfer request not found or access restricted.")

    can_decide, cant_decide_reason = can_user_decide_vacant_transfer(user, transfer_request)

    student = transfer_request.student
    current_occupancy = transfer_request.current_occupancy
    preferred_bed = transfer_request.preferred_bed

    # Live vacancy check of destination bed
    is_dest_vacant = False
    dest_operational = False
    if preferred_bed:
        dest_operational = (
            preferred_bed.is_active
            and preferred_bed.operational_status == OperationalStatus.IN_SERVICE
            and preferred_bed.room.is_active
            and preferred_bed.room.operational_status == OperationalStatus.IN_SERVICE
        )
        is_dest_vacant = dest_operational and not StudentOccupancy.objects.filter(
            bed=preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists()

    # Eligibility evaluation
    eligibility_report = check_student_exchange_eligibility(
        student=student,
        occupancy=current_occupancy,
    )

    # Source and destination hostels
    source_hostel = current_occupancy.bed.room.floor.block.hostel
    dest_hostel = preferred_bed.room.floor.block.hostel if preferred_bed else None
    is_cross_hostel = dest_hostel and (source_hostel.id != dest_hostel.id)

    # Approvals & Audit logs
    approvals = list(
        transfer_request.approvals.select_related("authority_user", "scope_hostel").order_by("-decided_at", "-created_at")
    )
    latest_approval = approvals[0] if approvals else None

    audit_logs = (
        AuditLog.objects.filter(
            institution=user.institution,
            target_id=str(transfer_request.id),
        )
        .select_related("acting_user")
        .order_by("timestamp")
    )

    # Check for transfer record if completed
    transfer_record = TransferRecord.objects.filter(exchange_request=transfer_request).first()

    context = {
        "transfer_request": transfer_request,
        "student": student,
        "current_occupancy": current_occupancy,
        "preferred_bed": preferred_bed,
        "source_hostel": source_hostel,
        "dest_hostel": dest_hostel,
        "is_cross_hostel": is_cross_hostel,
        "is_dest_vacant": is_dest_vacant,
        "dest_operational": dest_operational,
        "eligibility_report": eligibility_report,
        "can_decide": can_decide,
        "cant_decide_reason": cant_decide_reason,
        "approvals": approvals,
        "latest_approval": latest_approval,
        "transfer_record": transfer_record,
        "audit_logs": audit_logs,
        "is_dean": is_dean,
        "is_chief_warden": is_cw,
        "is_warden": is_w,
        "active_nav": "exchange_approvals",
    }
    return render(request, "exchange/transfer_approval_detail.html", context)


@login_required
@require_POST
def exchange_vacant_transfer_approval_decide_view(request, pk):
    """Handle authority decision (Approve & Execute / Reject) on a vacant-bed transfer."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)

    if not (is_w or is_cw):
        raise PermissionDenied("You do not have authority to decide transfer requests.")

    decision = request.POST.get("decision", "").strip().lower()
    remarks = request.POST.get("remarks", "").strip()

    if decision not in ("approved", "rejected"):
        messages.error(request, "Invalid decision value.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    if decision == "rejected" and not remarks:
        messages.error(request, "Remarks are mandatory when rejecting a transfer request.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    try:
        if decision == "approved":
            approval, transfer_record = approve_and_execute_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request approved and executed successfully. "
                f"Student {transfer_record.student.full_name} is now allocated to {transfer_record.destination_hostel_name}, "
                f"Room {transfer_record.destination_room_number}, Bed {transfer_record.destination_bed_label}.",
            )
        else:
            approval, req = reject_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request for {req.student.full_name} has been rejected. Student's current room allocation is unchanged.",
            )
    except ValidationError as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("exchange:transfer_approval_detail", pk=pk)


@login_required
def transfer_letter_view(request, pk):
    """
    Render or download the official RoomLoop Accommodation Transfer Letter for a completed transfer.
    Accessible by:
    - The transferred student.
    - Wardens, Chief Wardens, and Deans of the institution.
    """
    user = request.user
    transfer_record = (
        TransferRecord.objects.filter(Q(id=pk) | Q(exchange_request_id=pk))
        .select_related(
            "student",
            "institution",
            "approval__authority_user",
            "previous_room__floor__block__hostel",
            "previous_bed",
            "new_room__floor__block__hostel",
            "new_bed",
            "approved_by",
        )
        .first()
    )
    if not transfer_record:
        raise Http404("Transfer record not found.")

    is_owner = (user.id == transfer_record.student_id)
    is_staff = (
        user.institution_id == transfer_record.institution_id
        and (
            getattr(user, "is_warden_role", False)
            or getattr(user, "is_chief_warden_role", False)
            or getattr(user, "is_dean_role", False)
        )
    )

    if not (is_owner or is_staff):
        raise PermissionDenied("You do not have permission to view this transfer document.")

    download_format = request.GET.get("format") or request.GET.get("download")

    context = {
        "transfer": transfer_record,
        "student": transfer_record.student,
        "institution": transfer_record.institution,
        "approval": transfer_record.approval,
        "now": timezone.now(),
    }

    response = render(request, "exchange/transfer_letter.html", context)
    if download_format in ("1", "true", "download"):
        safe_reg = getattr(transfer_record.student, "registration_number", "student") or "student"
        response["Content-Disposition"] = f'attachment; filename="transfer_letter_{safe_reg}.html"'
    return response


    if not (is_w or is_cw or is_dean):
        raise PermissionDenied("You do not have permission to access transfer approvals.")

    transfer_request = get_vacant_transfer_detail(user, pk)
    if not transfer_request:
        raise Http404("Transfer request not found or access restricted.")

    can_decide, cant_decide_reason = can_user_decide_vacant_transfer(user, transfer_request)

    student = transfer_request.student
    current_occupancy = transfer_request.current_occupancy
    preferred_bed = transfer_request.preferred_bed

    # Live vacancy check of destination bed
    is_dest_vacant = False
    dest_operational = False
    if preferred_bed:
        dest_operational = (
            preferred_bed.is_active
            and preferred_bed.operational_status == OperationalStatus.IN_SERVICE
            and preferred_bed.room.is_active
            and preferred_bed.room.operational_status == OperationalStatus.IN_SERVICE
        )
        is_dest_vacant = dest_operational and not StudentOccupancy.objects.filter(
            bed=preferred_bed,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).exists()

    # Eligibility evaluation
    eligibility_report = check_student_exchange_eligibility(
        student=student,
        occupancy=current_occupancy,
    )

    # Source and destination hostels
    source_hostel = current_occupancy.bed.room.floor.block.hostel
    dest_hostel = preferred_bed.room.floor.block.hostel if preferred_bed else None
    is_cross_hostel = dest_hostel and (source_hostel.id != dest_hostel.id)

    # Approvals & Audit logs
    approvals = list(
        transfer_request.approvals.select_related("authority_user", "scope_hostel").order_by("-decided_at", "-created_at")
    )
    latest_approval = approvals[0] if approvals else None

    audit_logs = (
        AuditLog.objects.filter(
            institution=user.institution,
            target_id=str(transfer_request.id),
        )
        .select_related("acting_user")
        .order_by("timestamp")
    )

    # Check for transfer record if completed
    transfer_record = TransferRecord.objects.filter(exchange_request=transfer_request).first()

    context = {
        "transfer_request": transfer_request,
        "student": student,
        "current_occupancy": current_occupancy,
        "preferred_bed": preferred_bed,
        "source_hostel": source_hostel,
        "dest_hostel": dest_hostel,
        "is_cross_hostel": is_cross_hostel,
        "is_dest_vacant": is_dest_vacant,
        "dest_operational": dest_operational,
        "eligibility_report": eligibility_report,
        "can_decide": can_decide,
        "cant_decide_reason": cant_decide_reason,
        "approvals": approvals,
        "latest_approval": latest_approval,
        "transfer_record": transfer_record,
        "audit_logs": audit_logs,
        "is_dean": is_dean,
        "is_chief_warden": is_cw,
        "is_warden": is_w,
        "active_nav": "exchange_approvals",
    }
    return render(request, "exchange/transfer_approval_detail.html", context)


@login_required
@require_POST
def exchange_vacant_transfer_approval_decide_view(request, pk):
    """Handle authority decision (Approve & Execute / Reject) on a vacant-bed transfer."""
    user = request.user
    is_w = getattr(user, "is_warden_role", False)
    is_cw = getattr(user, "is_chief_warden_role", False)

    if not (is_w or is_cw):
        raise PermissionDenied("You do not have authority to decide transfer requests.")

    decision = request.POST.get("decision", "").strip().lower()
    remarks = request.POST.get("remarks", "").strip()

    if decision not in ("approved", "rejected"):
        messages.error(request, "Invalid decision value.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    if decision == "rejected" and not remarks:
        messages.error(request, "Remarks are mandatory when rejecting a transfer request.")
        return redirect("exchange:transfer_approval_detail", pk=pk)

    try:
        if decision == "approved":
            approval, transfer_record = approve_and_execute_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request approved and executed successfully. "
                f"Student {transfer_record.student.full_name} is now allocated to {transfer_record.destination_hostel_name}, "
                f"Room {transfer_record.destination_room_number}, Bed {transfer_record.destination_bed_label}.",
            )
        else:
            approval, req = reject_vacant_transfer(
                request_id=pk,
                actor=user,
                remarks=remarks,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Transfer request for {req.student.full_name} has been rejected. Student's current room allocation is unchanged.",
            )
    except ValidationError as exc:
        for msg in _validation_messages(exc):
            messages.error(request, msg)
    except PermissionDenied as exc:
        messages.error(request, str(exc))

    return redirect("exchange:transfer_approval_detail", pk=pk)


@login_required
def transfer_letter_view(request, pk):
    """
    Render or download the official RoomLoop Accommodation Transfer Letter for a completed transfer.
    Accessible by:
    - The transferred student.
    - Wardens, Chief Wardens, and Deans of the institution.
    """
    user = request.user
    transfer_record = (
        TransferRecord.objects.filter(Q(id=pk) | Q(exchange_request_id=pk))
        .select_related(
            "student",
            "institution",
            "approval__authority_user",
            "previous_room__floor__block__hostel",
            "previous_bed",
            "new_room__floor__block__hostel",
            "new_bed",
            "approved_by",
        )
        .first()
    )
    if not transfer_record:
        raise Http404("Transfer record not found.")

    is_owner = (user.id == transfer_record.student_id)
    is_staff = (
        user.institution_id == transfer_record.institution_id
        and (
            getattr(user, "is_warden_role", False)
            or getattr(user, "is_chief_warden_role", False)
            or getattr(user, "is_dean_role", False)
        )
    )

    if not (is_owner or is_staff):
        raise PermissionDenied("You do not have permission to view this transfer document.")

    download_format = request.GET.get("format") or request.GET.get("download")

    context = {
        "transfer": transfer_record,
        "student": transfer_record.student,
        "institution": transfer_record.institution,
        "approval": transfer_record.approval,
        "now": timezone.now(),
    }

    response = render(request, "exchange/transfer_letter.html", context)
    if download_format in ("1", "true", "download"):
        safe_reg = getattr(transfer_record.student, "registration_number", "student") or "student"
        response["Content-Disposition"] = f'attachment; filename="transfer_letter_{safe_reg}.html"'
    return response


