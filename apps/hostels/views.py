import json
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.urls import reverse
from django.db.models import Q, Count

from .models import (
    Hostel,
    Block,
    Floor,
    Room,
    Bed,
    StudentOccupancy,
    OperationalStatus,
)
from .forms import (
    HostelForm,
    RoomForm,
    BedStatusForm,
    OccupancyAssignForm,
    OccupancyReleaseForm,
)
from .selectors import (
    get_hostel_queryset_with_stats,
    get_room_inventory_queryset,
    get_vacant_beds,
    get_eligible_unallocated_students,
    get_hostel_builder_initial_data,
    calculate_configuration_metrics,
    get_student_active_occupancy,
    get_student_verified_eligibility,
    get_warden_assigned_hostel_ids,
)
from .services import (
    create_hostel,
    update_hostel,
    create_room,
    update_bed_status,
    assign_student_to_bed,
    release_student_occupancy,
    save_custom_hostel_configuration,
)
from apps.accounts.permissions import (
    can_manage_infrastructure,
    can_edit_hostel,
    can_manage_rooms,
    can_assign_occupancy,
    can_release_occupancy,
    can_update_bed_status,
    can_view_occupancy_roster,
)


@login_required
def hostel_list_view(request):
    """Hostel listing with real aggregate metrics, category tabs, and server-side eligibility enforcement."""
    user = request.user
    institution = user.institution

    status_filter = request.GET.get("status")
    category_filter = request.GET.get("category")
    search_query = request.GET.get("q", "").strip()

    base_qs = get_hostel_queryset_with_stats(institution=institution, user=user)

    # Counts for category tabs (lightweight count without executing heavy correlated subqueries)
    counts_qs = Hostel.objects.filter(institution=institution)
    if user.is_warden_role:
        from .selectors import get_warden_assigned_hostel_ids
        counts_qs = counts_qs.filter(id__in=get_warden_assigned_hostel_ids(user))
    category_counts = counts_qs.aggregate(
        total=Count("id"),
        boys=Count("id", filter=Q(resident_category=Hostel.ResidentCategory.MALE)),
        girls=Count("id", filter=Q(resident_category=Hostel.ResidentCategory.FEMALE)),
    )
    total_count = category_counts["total"]
    boys_count = category_counts["boys"]
    girls_count = category_counts["girls"]

    student_verified_eligibility = None
    is_eligibility_verified = False

    if user.is_student_role:
        student_verified_eligibility = get_student_verified_eligibility(user)
        is_eligibility_verified = student_verified_eligibility is not None

        if is_eligibility_verified:
            # Enforce server-side: student strictly sees hostels matching verified eligibility.
            # Query-string overrides (?category=female on male student) are denied server-side.
            hostels_qs = base_qs.filter(resident_category=student_verified_eligibility)
        else:
            # Residential eligibility unverified:
            # Provide read-only overview; bed selection and allocations are locked.
            hostels_qs = base_qs
    else:
        # Authorized staff: preserve full oversight and hostel-scoped access
        hostels_qs = base_qs
        if category_filter in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
            hostels_qs = hostels_qs.filter(resident_category=category_filter)

    if status_filter:
        hostels_qs = hostels_qs.filter(status=status_filter)
    if search_query:
        hostels_qs = hostels_qs.filter(
            Q(name__icontains=search_query) | Q(code__icontains=search_query)
        )

    hostels = list(hostels_qs)
    for h in hostels:
        m_beds = getattr(h, "maintenance_beds", 0)
        h.available_beds = max(0, h.total_beds - h.occupied_beds - m_beds)
        h.available_operational_beds = h.available_beds
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0

        # Human-readable occupancy label
        if h.total_beds == 0:
            h.occupancy_status_label = "Unconfigured"
        elif h.available_beds == 0:
            h.occupancy_status_label = "Fully Occupied"
        elif h.occupancy_rate >= 80:
            h.occupancy_status_label = f"High Occupancy ({h.available_beds} left)"
        elif h.occupied_beds > 0:
            h.occupancy_status_label = f"Moderate ({h.available_beds} available)"
        else:
            h.occupancy_status_label = f"Vacant ({h.available_beds} available)"

        if user.is_student_role:
            h.is_student_eligible = is_eligibility_verified and (h.resident_category == student_verified_eligibility)
        else:
            h.is_student_eligible = True

    return render(
        request,
        "hostels/hostel_list.html",
        {
            "hostels": hostels,
            "status_filter": status_filter,
            "category_filter": category_filter,
            "search_query": search_query,
            "total_count": total_count,
            "boys_count": boys_count,
            "girls_count": girls_count,
            "student_verified_eligibility": student_verified_eligibility,
            "is_eligibility_verified": is_eligibility_verified,
            "active_nav": "hostels",
        },
    )


@login_required
def hostel_detail_view(request, pk):
    """
    Hostel details view with interactive block & floor navigation,
    on-demand floor room/bed loading, server-side eligibility enforcement,
    and student privacy protection.
    """
    user = request.user
    institution = user.institution

    hostel_qs = get_hostel_queryset_with_stats(institution=institution, user=user).filter(pk=pk)
    hostel = get_object_or_404(hostel_qs)

    # Server-side student eligibility authorization check
    student_verified_eligibility = None
    can_select_beds = True

    if user.is_student_role:
        student_verified_eligibility = get_student_verified_eligibility(user)
        if student_verified_eligibility:
            if hostel.resident_category != student_verified_eligibility:
                raise PermissionDenied(
                    f"Access Denied: You are not authorized to view or select beds in this hostel. "
                    f"Your verified residential eligibility is '{user.get_residential_eligibility_display()}'."
                )
        else:
            # Unverified student: viewable in read-only overview, bed selection disabled
            can_select_beds = False

    m_beds = getattr(hostel, "maintenance_beds", 0)
    hostel.available_beds = max(0, hostel.total_beds - hostel.occupied_beds - m_beds)
    hostel.available_operational_beds = hostel.available_beds
    hostel.occupancy_rate = round((hostel.occupied_beds / hostel.total_beds * 100), 1) if hostel.total_beds > 0 else 0

    if hostel.total_beds == 0:
        hostel.occupancy_status_label = "Unconfigured"
    elif hostel.available_beds == 0:
        hostel.occupancy_status_label = "Fully Occupied"
    elif hostel.occupancy_rate >= 80:
        hostel.occupancy_status_label = f"High Occupancy ({hostel.available_beds} left)"
    elif hostel.occupied_beds > 0:
        hostel.occupancy_status_label = f"Moderate ({hostel.available_beds} available)"
    else:
        hostel.occupancy_status_label = f"Vacant ({hostel.available_beds} available)"

    # Blocks navigation
    blocks = list(
        Block.objects.filter(hostel=hostel, is_active=True)
        .prefetch_related("floors")
        .order_by("name")
    )

    selected_block = None
    block_id = request.GET.get("block")
    if block_id:
        selected_block = next((b for b in blocks if str(b.id) == str(block_id)), None)
    if not selected_block and blocks:
        selected_block = blocks[0]

    # Active floors for the selected block
    floors = []
    selected_floor = None
    if selected_block:
        floors = list(selected_block.floors.filter(is_active=True).order_by("floor_number"))
        floor_id = request.GET.get("floor")
        if floor_id:
            selected_floor = next((f for f in floors if str(f.id) == str(floor_id)), None)
        if not selected_floor and floors:
            selected_floor = floors[0]

    # Floor rooms & beds loaded ON DEMAND (performance: max 20 rooms per floor instead of campus-wide Cartesian loads)
    rooms_data = []
    floor_stats = {
        "total_rooms": 0,
        "total_beds": 0,
        "occupied_beds": 0,
        "available_beds": 0,
        "maintenance_beds": 0,
    }

    can_view_student_details = can_view_occupancy_roster(user)

    if selected_floor:
        rooms = list(
            Room.objects.filter(floor=selected_floor, is_active=True)
            .prefetch_related("beds")
            .order_by("room_number")
        )

        active_occupancies = {
            occ.bed_id: occ
            for occ in StudentOccupancy.objects.filter(
                bed__room__floor=selected_floor,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).select_related("student")
        }

        floor_stats["total_rooms"] = len(rooms)
        for room in rooms:
            room_beds = [b for b in room.beds.all() if b.is_active]
            beds_list = []
            room_occupied_count = 0
            room_available_count = 0
            room_maint_count = 0

            for bed in sorted(room_beds, key=lambda b: b.bed_label):
                occ = active_occupancies.get(bed.id)
                is_occupied = occ is not None
                is_maintenance = bed.operational_status == OperationalStatus.MAINTENANCE

                if is_occupied:
                    room_occupied_count += 1
                    status_type = "occupied"
                    if can_view_student_details:
                        occupant_name = occ.student.full_name
                        occupant_reg = occ.student.registration_number or ""
                    else:
                        occupant_name = "Occupied"
                        occupant_reg = ""
                elif is_maintenance:
                    room_maint_count += 1
                    status_type = "maintenance"
                    occupant_name = None
                    occupant_reg = None
                else:
                    room_available_count += 1
                    status_type = "available"
                    occupant_name = None
                    occupant_reg = None

                beds_list.append({
                    "id": str(bed.id),
                    "bed_label": bed.bed_label,
                    "operational_status": bed.operational_status,
                    "status_type": status_type,
                    "is_occupied": is_occupied,
                    "is_maintenance": is_maintenance,
                    "is_available": (not is_occupied and not is_maintenance and bed.operational_status == OperationalStatus.IN_SERVICE),
                    "occupant_name": occupant_name,
                    "occupant_reg": occupant_reg,
                })

            floor_stats["total_beds"] += len(room_beds)
            floor_stats["occupied_beds"] += room_occupied_count
            floor_stats["available_beds"] += room_available_count
            floor_stats["maintenance_beds"] += room_maint_count

            rooms_data.append({
                "room": room,
                "beds": beds_list,
                "total_beds": len(room_beds),
                "occupied_beds": room_occupied_count,
                "available_beds": room_available_count,
                "maintenance_beds": room_maint_count,
            })

    assigned_wardens = hostel.assigned_wardens.select_related("warden").all()

    return render(
        request,
        "hostels/hostel_detail.html",
        {
            "hostel": hostel,
            "blocks": blocks,
            "selected_block": selected_block,
            "floors": floors,
            "selected_floor": selected_floor,
            "rooms_data": rooms_data,
            "floor_stats": floor_stats,
            "assigned_wardens": assigned_wardens,
            "can_view_student_details": can_view_student_details,
            "can_select_beds": can_select_beds,
            "student_verified_eligibility": student_verified_eligibility,
            "active_nav": "hostels",
        },
    )


@login_required
def hostel_builder_view(request, pk=None):
    """
    Phase 2 Custom Hostel Builder:
    Interactive, responsive, full-inventory workflow for Dean of Student Welfare and Hostel Administrator.
    Supports configuring custom blocks, independent floor numbers, variable room counts per floor,
    and variable bed counts per room (1, 2, 3, 4, 5, 6, dormitory, custom).
    Enforces safe editing, child ID anti-tampering, stale edit detection, and atomic persistence.
    """
    user = request.user
    if not can_manage_infrastructure(user):
        raise PermissionDenied("Only the Dean of Student Welfare and authorized Hostel Administrators can access the Custom Hostel Builder.")

    institution = user.institution
    hostel = None
    is_edit = pk is not None

    if is_edit:
        hostel = get_object_or_404(Hostel, pk=pk, institution=institution)
        if not can_edit_hostel(user, hostel):
            raise PermissionDenied("You do not have permission to edit this hostel.")

    if request.method == "POST":
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("Content-Type", "")
        raw_json = None
        change_reason = ""

        if is_ajax and request.body:
            try:
                body_data = json.loads(request.body.decode("utf-8"))
                raw_json = json.dumps(body_data)
                config_data = body_data
                change_reason = body_data.get("change_reason", "")
            except json.JSONDecodeError:
                return JsonResponse({"success": False, "error": "Malformed JSON payload."}, status=400)
        else:
            raw_json = request.POST.get("configuration_json", "")
            change_reason = request.POST.get("change_reason", "")
            try:
                config_data = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                config_data = {}

        if not config_data:
            err_msg = "No configuration data received. Please complete the builder steps."
            if is_ajax:
                return JsonResponse({"success": False, "error": err_msg}, status=400)
            messages.error(request, err_msg)
            initial_data = get_hostel_builder_initial_data(hostel)
            return render(request, "hostels/hostel_builder.html", {
                "hostel": hostel,
                "is_edit": is_edit,
                "initial_config_json": json.dumps(initial_data),
                "resident_categories": Hostel.ACTIVE_RESIDENT_CATEGORIES,
                "operational_statuses": OperationalStatus.choices,
                "hostel_statuses": Hostel.Status.choices,
                "active_nav": "hostels",
            })

        try:
            saved_hostel = save_custom_hostel_configuration(
                institution=institution,
                hostel=hostel,
                configuration_data=config_data,
                acting_user=user,
                change_reason=change_reason,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            success_msg = f"Hostel '{saved_hostel.name}' ({saved_hostel.code}) configuration saved successfully."
            if is_ajax:
                metrics = calculate_configuration_metrics(config_data)
                return JsonResponse({
                    "success": True,
                    "message": success_msg,
                    "hostel_id": saved_hostel.id,
                    "metrics": metrics,
                    "redirect_url": reverse("hostels:hostel_detail", kwargs={"pk": saved_hostel.id}),
                })
            messages.success(request, success_msg)
            return redirect("hostels:hostel_detail", pk=saved_hostel.id)
        except ValidationError as e:
            err_msg = e.message if hasattr(e, "message") else ("; ".join(e.messages) if hasattr(e, "messages") else str(e))
            if is_ajax:
                return JsonResponse({"success": False, "error": err_msg}, status=400)
            messages.error(request, err_msg)
            # Re-render preserving the user's submitted configuration so no work is lost
            return render(request, "hostels/hostel_builder.html", {
                "hostel": hostel,
                "is_edit": is_edit,
                "initial_config_json": raw_json or json.dumps(config_data),
                "error_message": err_msg,
                "resident_categories": Hostel.ACTIVE_RESIDENT_CATEGORIES,
                "operational_statuses": OperationalStatus.choices,
                "hostel_statuses": Hostel.Status.choices,
                "active_nav": "hostels",
            })

    # GET request: load initial data
    initial_data = get_hostel_builder_initial_data(hostel)
    return render(
        request,
        "hostels/hostel_builder.html",
        {
            "hostel": hostel,
            "is_edit": is_edit,
            "initial_config_json": json.dumps(initial_data),
            "resident_categories": Hostel.ACTIVE_RESIDENT_CATEGORIES,
            "operational_statuses": OperationalStatus.choices,
            "hostel_statuses": Hostel.Status.choices,
            "active_nav": "hostels",
        },
    )


@login_required
def hostel_create_view(request):
    """Serve the Custom Hostel Builder for comprehensive creation."""
    return hostel_builder_view(request)


@login_required
def hostel_edit_view(request, pk):
    """Serve the Custom Hostel Builder for safe infrastructure editing."""
    return hostel_builder_view(request, pk=pk)


@login_required
def room_inventory_view(request):
    """Searchable, filterable room inventory table."""
    user = request.user
    institution = user.institution

    hostel_id = request.GET.get("hostel")
    block_id = request.GET.get("block")
    floor_id = request.GET.get("floor")
    room_type = request.GET.get("room_type")
    availability = request.GET.get("availability")
    operational_status = request.GET.get("status")
    search_query = request.GET.get("q", "").strip()

    rooms_qs = get_room_inventory_queryset(
        institution=institution,
        hostel_id=hostel_id,
        block_id=block_id,
        floor_id=floor_id,
        room_type=room_type,
        operational_status=operational_status,
        search_query=search_query,
        availability=availability,
        user=user,
    )

    if user.is_student_role:
        student_verified_eligibility = get_student_verified_eligibility(user)
        if student_verified_eligibility:
            rooms_qs = rooms_qs.filter(floor__block__hostel__resident_category=student_verified_eligibility)
        else:
            rooms_qs = rooms_qs.none()

    paginator = Paginator(rooms_qs, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Filter dropdown lists
    if user.is_warden_role:
        assigned_ids = get_warden_assigned_hostel_ids(user)
        filter_hostels = Hostel.objects.filter(id__in=assigned_ids)
    elif institution:
        filter_hostels = Hostel.objects.filter(institution=institution)
    else:
        filter_hostels = Hostel.objects.all()

    return render(
        request,
        "hostels/room_inventory.html",
        {
            "page_obj": page_obj,
            "filter_hostels": filter_hostels,
            "selected_hostel": hostel_id,
            "selected_room_type": room_type,
            "selected_availability": availability,
            "selected_status": operational_status,
            "search_query": search_query,
            "active_nav": "rooms",
        },
    )


@login_required
def room_create_view(request):
    """Create a new room (authorized: Dean, Hostel Admin, Chief Warden)."""
    user = request.user
    if not can_manage_rooms(user):
        raise PermissionDenied("Only authorized administrators and the Dean can create new rooms.")

    if request.method == "POST":
        form = RoomForm(request.POST, institution=user.institution)
        if form.is_valid():
            try:
                room = create_room(
                    floor=form.cleaned_data["floor"],
                    room_number=form.cleaned_data["room_number"],
                    room_type=form.cleaned_data["room_type"],
                    occupancy_category=form.cleaned_data["occupancy_category"],
                    accessibility_attributes=form.cleaned_data["accessibility_attributes"],
                    description=form.cleaned_data["description"],
                    operational_status=form.cleaned_data["operational_status"],
                    create_beds=form.cleaned_data["create_default_beds"],
                    acting_user=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, f"Room {room.room_number} created successfully.")
                return redirect("hostels:room_detail", pk=room.id)
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, "message") else str(e))
    else:
        form = RoomForm(institution=user.institution)

    return render(
        request,
        "hostels/room_form.html",
        {"form": form, "active_nav": "rooms"},
    )


@login_required
def room_detail_view(request, pk):
    """Room detail with beds and occupancy status. Scoped privacy rules."""
    user = request.user
    room = get_object_or_404(
        Room.objects.select_related("floor__block__hostel__institution"),
        pk=pk,
    )

    # Institution scoping check
    if user.institution and room.floor.block.hostel.institution_id != user.institution.id:
        raise PermissionDenied("You do not have permission to view rooms outside your institution.")

    # Warden scoping check
    if user.is_warden_role:
        from .models import WardenAssignment
        if not WardenAssignment.objects.filter(warden=user, hostel=room.floor.block.hostel).exists():
            raise PermissionDenied("You are not assigned to manage this hostel.")

    # Student residential category check
    if user.is_student_role:
        student_verified_eligibility = get_student_verified_eligibility(user)
        if student_verified_eligibility and room.floor.block.hostel.resident_category != student_verified_eligibility:
            raise PermissionDenied("You are not eligible to view rooms in this hostel.")

    beds = (
        Bed.objects.filter(room=room)
        .prefetch_related("occupancies", "occupancies__student")
        .order_by("bed_label")
    )

    # Attach active occupancy and privacy filters
    is_privileged = user.is_warden_role or user.is_chief_warden_role or user.is_hostel_admin_role or user.is_superuser
    bed_data = []
    for bed in beds:
        active_occ = bed.occupancies.filter(status=StudentOccupancy.OccupancyStatus.ACTIVE).first()
        bed_data.append({
            "bed": bed,
            "active_occupancy": active_occ,
            # Ordinary students see whether a bed is occupied, but not other students' private identities
            "show_occupant_details": is_privileged or (active_occ and active_occ.student_id == user.id),
        })

    return render(
        request,
        "hostels/room_detail.html",
        {
            "room": room,
            "bed_data": bed_data,
            "is_privileged": is_privileged,
            "active_nav": "rooms",
        },
    )


@login_required
def bed_list_view(request):
    """Bed inventory management view."""
    user = request.user
    if user.is_student_role:
        raise PermissionDenied("Students are not permitted to browse administrative bed rosters.")

    institution = user.institution
    operational_status = request.GET.get("status")
    search_query = request.GET.get("q", "").strip()

    if user.is_warden_role:
        assigned_hostels = get_warden_assigned_hostel_ids(user)
        beds_qs = Bed.objects.filter(room__floor__block__hostel_id__in=assigned_hostels)
    elif institution:
        beds_qs = Bed.objects.filter(room__floor__block__hostel__institution=institution)
    else:
        beds_qs = Bed.objects.all()
    beds_qs = beds_qs.select_related("room__floor__block__hostel").prefetch_related("occupancies")

    if operational_status:
        beds_qs = beds_qs.filter(operational_status=operational_status)
    if search_query:
        beds_qs = beds_qs.filter(
            Q(bed_label__icontains=search_query)
            | Q(room__room_number__icontains=search_query)
            | Q(room__floor__block__hostel__name__icontains=search_query)
        )

    paginator = Paginator(beds_qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "hostels/bed_list.html",
        {
            "page_obj": page_obj,
            "selected_status": operational_status,
            "search_query": search_query,
            "active_nav": "beds",
        },
    )


@login_required
def bed_status_update_view(request, pk):
    """Update bed status (e.g. Under Maintenance, In Service)."""
    user = request.user
    bed = get_object_or_404(Bed, pk=pk)
    if not can_update_bed_status(user, bed):
        raise PermissionDenied("Only authorized staff can update bed status.")

    if request.method == "POST":
        new_status = request.POST.get("operational_status")
        try:
            update_bed_status(
                bed=bed,
                new_status=new_status,
                acting_user=user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, f"Bed {bed} status updated to '{bed.get_operational_status_display()}'.")
        except ValidationError as e:
            messages.error(request, e.message if hasattr(e, "message") else str(e))

    return redirect(request.META.get("HTTP_REFERER", reverse("hostels:room_detail", kwargs={"pk": bed.room.id})))


@login_required
def occupancy_list_view(request):
    """Roster of student occupancy records with search and status filtering."""
    user = request.user
    if not can_view_occupancy_roster(user):
        raise PermissionDenied("Students cannot browse overall occupancy rosters.")

    institution = user.institution
    status_filter = request.GET.get("status", "active")
    search_query = request.GET.get("q", "").strip()

    qs = (
        StudentOccupancy.objects.all()
        .select_related("student", "bed__room__floor__block__hostel", "institution")
        .order_by("-start_date", "-created_at")
    )
    if institution:
        qs = qs.filter(institution=institution)

    if user.is_warden_role:
        assigned_hostels = get_warden_assigned_hostel_ids(user)
        qs = qs.filter(bed__room__floor__block__hostel_id__in=assigned_hostels)

    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)
    if search_query:
        qs = qs.filter(
            Q(student__full_name__icontains=search_query)
            | Q(student__email__icontains=search_query)
            | Q(student__registration_number__icontains=search_query)
            | Q(bed__room__room_number__icontains=search_query)
        )

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "hostels/occupancy_list.html",
        {
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search_query": search_query,
            "active_nav": "occupancy",
        },
    )


@login_required
def occupancy_assign_view(request):
    """Authorized view to allocate a student to an available bed."""
    user = request.user
    if not can_assign_occupancy(user):
        raise PermissionDenied("You are not authorized to allocate student accommodation.")

    institution = user.institution

    if request.method == "POST":
        form = OccupancyAssignForm(request.POST, institution=institution, user=user)
        if form.is_valid():
            try:
                occupancy = assign_student_to_bed(
                    student=form.cleaned_data["student"],
                    bed=form.cleaned_data["bed"],
                    start_date=form.cleaned_data["start_date"],
                    source=form.cleaned_data["source"],
                    created_by=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(
                    request,
                    f"Successfully allocated {occupancy.student.full_name} to Room {occupancy.bed.room.room_number} ({occupancy.bed.bed_label})!",
                )
                return redirect("hostels:occupancy_list")
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, "message") else str(e))
    else:
        form = OccupancyAssignForm(institution=institution, user=user)

    return render(
        request,
        "hostels/occupancy_assign.html",
        {"form": form, "active_nav": "occupancy"},
    )


@login_required
def occupancy_detail_view(request, pk):
    """View details of an allocation record."""
    user = request.user
    occupancy = get_object_or_404(
        StudentOccupancy.objects.select_related(
            "student",
            "bed__room__floor__block__hostel__institution",
            "created_by",
            "institution",
        ),
        pk=pk,
    )

    # Privacy check
    if user.is_student_role and occupancy.student_id != user.id:
        raise PermissionDenied("You can only view your own occupancy records.")

    # Warden assignment check
    if user.is_warden_role:
        from .models import WardenAssignment
        if not WardenAssignment.objects.filter(warden=user, hostel=occupancy.bed.room.floor.block.hostel).exists():
            raise PermissionDenied("You are not assigned to manage this hostel.")

    return render(
        request,
        "hostels/occupancy_detail.html",
        {"occupancy": occupancy, "active_nav": "occupancy"},
    )


@login_required
def occupancy_release_view(request, pk):
    """Release an active student allocation with a mandatory reason."""
    user = request.user
    occupancy = get_object_or_404(StudentOccupancy, pk=pk)
    if not can_release_occupancy(user, occupancy):
        raise PermissionDenied("Only authorized staff can release student allocations.")

    if request.method == "POST":
        form = OccupancyReleaseForm(request.POST)
        if form.is_valid():
            try:
                release_student_occupancy(
                    occupancy_id=occupancy.id,
                    release_reason=form.cleaned_data["release_reason"],
                    end_date=form.cleaned_data["end_date"],
                    acting_user=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(
                    request,
                    f"Successfully released allocation for {occupancy.student.full_name}.",
                )
                return redirect("hostels:occupancy_list")
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, "message") else str(e))
    else:
        form = OccupancyReleaseForm()

    return render(
        request,
        "hostels/occupancy_release.html",
        {"form": form, "occupancy": occupancy, "active_nav": "occupancy"},
    )
