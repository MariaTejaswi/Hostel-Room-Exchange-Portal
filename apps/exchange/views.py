from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.hostels.models import (
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    StudentOccupancy,
)

from .forms import ExchangeRequestForm
from .models import Notification
from .selectors import (
    get_student_exchange_requests,
    get_student_target_beds,
    get_student_target_rooms,
)
from .services import (
    create_exchange_request,
    update_exchange_request,
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


def _bed_browser_context(request, student, form):
    """Build the database-backed hostel/block/floor/room/bed browser state."""
    target_beds = get_student_target_beds(student)
    selected_bed_id = (
        request.POST.get("preferred_bed")
        or request.GET.get("bed_id")
        or getattr(form.instance, "preferred_bed_id", None)
    )
    selected_bed = target_beds.filter(pk=selected_bed_id).first() if selected_bed_id else None

    hostels = Hostel.objects.filter(
        institution=student.institution,
        status=Hostel.Status.ACTIVE,
        resident_category=student.residential_eligibility,
    ).order_by("name")
    selected_hostel_id = request.GET.get("hostel_id") or (
        selected_bed.room.floor.block.hostel_id if selected_bed else None
    )
    selected_hostel = hostels.filter(pk=selected_hostel_id).first() if selected_hostel_id else hostels.first()

    blocks = Block.objects.filter(
        hostel=selected_hostel,
        is_active=True,
    ).order_by("name") if selected_hostel else Block.objects.none()
    selected_block_id = request.GET.get("block_id") or (
        selected_bed.room.floor.block_id if selected_bed else None
    )
    selected_block = blocks.filter(pk=selected_block_id).first() if selected_block_id else blocks.first()

    floors = Floor.objects.filter(
        block=selected_block,
        is_active=True,
    ).order_by("floor_number") if selected_block else Floor.objects.none()
    selected_floor_id = request.GET.get("floor_id") or (
        selected_bed.room.floor_id if selected_bed else None
    )
    selected_floor = floors.filter(pk=selected_floor_id).first() if selected_floor_id else floors.first()

    selected_room_id = request.GET.get("room_id") or (
        selected_bed.room_id if selected_bed else None
    )
    rooms = get_student_target_rooms(
        student,
        floor_id=selected_floor.id if selected_floor else None,
        room_id=selected_room_id,
    )
    current_occupancy = (
        StudentOccupancy.objects.filter(
            student=student,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        .values_list("bed_id", flat=True)
        .first()
    )
    browser_rooms = []
    target_bed_ids = set(target_beds.values_list("id", flat=True))
    for room in rooms:
        browser_beds = []
        room_unavailable = (
            not room.is_active
            or room.operational_status != OperationalStatus.IN_SERVICE
            or not room.floor.is_active
            or not room.floor.block.is_active
            or room.floor.block.hostel.status != Hostel.Status.ACTIVE
        )
        for bed in room.beds.all():
            occupied = any(
                occupancy.status == StudentOccupancy.OccupancyStatus.ACTIVE
                for occupancy in bed.occupancies.all()
            )
            unavailable = (
                room_unavailable
                or not bed.is_active
                or bed.operational_status != OperationalStatus.IN_SERVICE
                or bed.id not in target_bed_ids
            )
            is_selected = str(bed.id) == str(selected_bed_id) and not unavailable
            browser_beds.append(
                {
                    "bed": bed,
                    "occupied": occupied,
                    "unavailable": unavailable,
                    "selectable": not unavailable and bed.id != current_occupancy,
                    "selected": is_selected,
                    "current": bed.id == current_occupancy,
                }
            )
        browser_rooms.append({"room": room, "beds": browser_beds})

    return {
        "browser_hostels": hostels,
        "browser_blocks": blocks,
        "browser_floors": floors,
        "browser_rooms": browser_rooms,
        "selected_hostel": selected_hostel,
        "selected_block": selected_block,
        "selected_floor": selected_floor,
        "selected_bed": selected_bed,
        "selected_bed_id": selected_bed_id,
        "selected_room_id": selected_room_id,
    }


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
    return render(
        request,
        "exchange/request_form.html",
        {
            "form": form,
            "page_title": "Create Exchange Request",
            "active_nav": "exchange",
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
    return render(
        request,
        "exchange/request_detail.html",
        {
            "exchange_request": exchange_request,
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
    browser_context = _bed_browser_context(request, request.user, form)
    return render(
        request,
        "exchange/request_form.html",
        {
            "form": form,
            "exchange_request": exchange_request,
            "page_title": "Edit Exchange Request",
            "active_nav": "exchange",
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
