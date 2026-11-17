from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
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
from .models import ExchangeRequest, Notification, PolicyRule
from .selectors import (
    get_eligible_blocks_for_hostel,
    get_eligible_floors_for_block,
    get_eligible_hostels_for_exchange,
    get_floor_room_summaries_for_exchange,
    get_room_bed_details_for_exchange,
    get_student_exchange_requests,
    get_student_target_beds,
    get_student_target_rooms,
)
from .services import (
    create_exchange_request,
    get_institution_exchange_policies,
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
    can_modify = exchange_request.status == ExchangeRequest.Status.OPEN and not is_expired and not has_active_proposal

    return render(
        request,
        "exchange/request_detail.html",
        {
            "exchange_request": exchange_request,
            "can_modify": can_modify,
            "is_expired": is_expired,
            "has_active_proposal": has_active_proposal,
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

