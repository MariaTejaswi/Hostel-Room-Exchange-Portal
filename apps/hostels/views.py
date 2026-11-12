from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.urls import reverse

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
)
from .services import (
    create_hostel,
    update_hostel,
    create_room,
    update_bed_status,
    assign_student_to_bed,
    release_student_occupancy,
)


@login_required
def hostel_list_view(request):
    """Hostel listing with real aggregate metrics and search filters."""
    user = request.user
    institution = user.institution

    status_filter = request.GET.get("status")
    search_query = request.GET.get("q", "").strip()

    hostels_qs = get_hostel_queryset_with_stats(institution=institution, user=user)

    if status_filter:
        hostels_qs = hostels_qs.filter(status=status_filter)
    if search_query:
        hostels_qs = hostels_qs.filter(name__icontains=search_query)

    hostels = list(hostels_qs)
    for h in hostels:
        h.available_beds = max(0, h.total_beds - h.occupied_beds)
        h.occupancy_rate = round((h.occupied_beds / h.total_beds * 100), 1) if h.total_beds > 0 else 0

    return render(
        request,
        "hostels/hostel_list.html",
        {
            "hostels": hostels,
            "status_filter": status_filter,
            "search_query": search_query,
            "active_nav": "hostels",
        },
    )


@login_required
def hostel_detail_view(request, pk):
    """Hostel details view with blocks, floors, rooms, and assigned wardens."""
    user = request.user
    institution = user.institution

    hostel_qs = get_hostel_queryset_with_stats(institution=institution, user=user).filter(pk=pk)
    hostel = get_object_or_404(hostel_qs)

    hostel.available_beds = max(0, hostel.total_beds - hostel.occupied_beds)
    hostel.occupancy_rate = round((hostel.occupied_beds / hostel.total_beds * 100), 1) if hostel.total_beds > 0 else 0

    blocks = (
        Block.objects.filter(hostel=hostel)
        .prefetch_related("floors", "floors__rooms")
        .order_by("name")
    )
    assigned_wardens = hostel.assigned_wardens.select_related("warden").all()

    return render(
        request,
        "hostels/hostel_detail.html",
        {
            "hostel": hostel,
            "blocks": blocks,
            "assigned_wardens": assigned_wardens,
            "active_nav": "hostels",
        },
    )


@login_required
def hostel_create_view(request):
    """Create a new hostel (restricted to Hostel Admin & System Admin)."""
    user = request.user
    if not (user.is_hostel_admin_role or user.is_superuser):
        raise PermissionDenied("Only administrators can create new hostels.")

    if request.method == "POST":
        form = HostelForm(request.POST)
        if form.is_valid():
            try:
                hostel = create_hostel(
                    institution=user.institution,
                    name=form.cleaned_data["name"],
                    code=form.cleaned_data["code"],
                    resident_category=form.cleaned_data["resident_category"],
                    address=form.cleaned_data["address"],
                    description=form.cleaned_data["description"],
                    status=form.cleaned_data["status"],
                    acting_user=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, f"Hostel '{hostel.name}' was created successfully.")
                return redirect("hostels:hostel_detail", pk=hostel.id)
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, "message") else str(e))
    else:
        form = HostelForm()

    return render(
        request,
        "hostels/hostel_form.html",
        {"form": form, "is_edit": False, "active_nav": "hostels"},
    )


@login_required
def hostel_edit_view(request, pk):
    """Edit hostel details."""
    user = request.user
    if not (user.is_hostel_admin_role or user.is_superuser):
        raise PermissionDenied("Only administrators can edit hostel properties.")

    hostel = get_object_or_404(Hostel, pk=pk, institution=user.institution)

    if request.method == "POST":
        form = HostelForm(request.POST, instance=hostel)
        if form.is_valid():
            try:
                update_hostel(
                    hostel=hostel,
                    name=form.cleaned_data["name"],
                    resident_category=form.cleaned_data["resident_category"],
                    address=form.cleaned_data["address"],
                    description=form.cleaned_data["description"],
                    status=form.cleaned_data["status"],
                    acting_user=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, f"Hostel '{hostel.name}' updated successfully.")
                return redirect("hostels:hostel_detail", pk=hostel.id)
            except ValidationError as e:
                form.add_error(None, e.message if hasattr(e, "message") else str(e))
    else:
        form = HostelForm(instance=hostel)

    return render(
        request,
        "hostels/hostel_form.html",
        {"form": form, "hostel": hostel, "is_edit": True, "active_nav": "hostels"},
    )


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

    paginator = Paginator(rooms_qs, 15)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Filter dropdown lists
    filter_hostels = Hostel.objects.filter(institution=institution) if institution else Hostel.objects.all()

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
    """Create a new room (restricted to Hostel Admin & Sysadmin)."""
    user = request.user
    if not (user.is_hostel_admin_role or user.is_superuser):
        raise PermissionDenied("Only administrators can create new rooms.")

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
    if user.is_warden_role and not (user.is_chief_warden_role or user.is_hostel_admin_role):
        from .models import WardenAssignment
        if not WardenAssignment.objects.filter(warden=user, hostel=room.floor.block.hostel).exists():
            raise PermissionDenied("You are not assigned to manage this hostel.")

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
    institution = user.institution
    operational_status = request.GET.get("status")
    search_query = request.GET.get("q", "").strip()

    beds_qs = (
        Bed.objects.filter(room__floor__block__hostel__institution=institution)
        if institution
        else Bed.objects.all()
    )
    beds_qs = beds_qs.select_related("room__floor__block__hostel").prefetch_related("occupancies")

    if operational_status:
        beds_qs = beds_qs.filter(operational_status=operational_status)
    if search_query:
        beds_qs = beds_qs.filter(
            models.Q(bed_label__icontains=search_query)
            | models.Q(room__room_number__icontains=search_query)
            | models.Q(room__floor__block__hostel__name__icontains=search_query)
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
    if not (user.is_hostel_admin_role or user.is_warden_role or user.is_superuser):
        raise PermissionDenied("Only authorized staff can update bed status.")

    bed = get_object_or_404(Bed, pk=pk)

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
    if user.is_student_role and not (user.is_staff or user.is_superuser):
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

    if user.is_warden_role and not (user.is_chief_warden_role or user.is_hostel_admin_role):
        from .models import WardenAssignment
        assigned_hostels = WardenAssignment.objects.filter(warden=user).values_list("hostel_id", flat=True)
        qs = qs.filter(bed__room__floor__block__hostel_id__in=assigned_hostels)

    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)
    if search_query:
        qs = qs.filter(
            models.Q(student__full_name__icontains=search_query)
            | models.Q(student__email__icontains=search_query)
            | models.Q(student__registration_number__icontains=search_query)
            | models.Q(bed__room__room_number__icontains=search_query)
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
    if not (user.is_hostel_admin_role or user.is_warden_role or user.is_superuser):
        raise PermissionDenied("You are not authorized to allocate student accommodation.")

    institution = user.institution

    if request.method == "POST":
        form = OccupancyAssignForm(request.POST, institution=institution)
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
        form = OccupancyAssignForm(institution=institution)

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

    return render(
        request,
        "hostels/occupancy_detail.html",
        {"occupancy": occupancy, "active_nav": "occupancy"},
    )


@login_required
def occupancy_release_view(request, pk):
    """Release an active student allocation with a mandatory reason."""
    user = request.user
    if not (user.is_hostel_admin_role or user.is_warden_role or user.is_superuser):
        raise PermissionDenied("Only authorized staff can release student allocations.")

    occupancy = get_object_or_404(StudentOccupancy, pk=pk)

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
