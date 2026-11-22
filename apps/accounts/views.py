import csv
import os
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.tokens import default_token_generator
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.db.models import Q, Count
from django.http import HttpResponse, JsonResponse
from django.utils.http import urlsafe_base64_decode

from .forms import (
    LoginForm,
    UserProfileForm,
    StudentCreateForm,
    StudentEditForm,
    StudentStatusToggleForm,
    EligibilityVerificationForm,
    StudentCSVUploadForm,
    PasswordSetupForm,
)
from .models import User
from .permissions import can_manage_student_accounts, can_verify_residential_eligibility
from .selectors import (
    get_dashboard_url_for_user,
    get_student_directory_queryset,
    get_pending_eligibility_students_queryset,
    get_pending_eligibility_count,
    get_student_detail_context,
)
from .services import (
    ROLE_STUDENT,
    create_student_account,
    update_student_account,
    set_student_account_status,
    initiate_student_password_setup,
    complete_student_password_setup,
    verify_student_eligibility,
    validate_csv_student_import,
    process_csv_student_import,
    update_user_profile,
)
from apps.core.models import AuditLog


def login_view(request):
    """User login view supporting session persistence, role redirection, and configurable demo shortcuts."""
    if request.user.is_authenticated:
        return redirect(get_dashboard_url_for_user(request.user))

    # Keep the public login URL canonical. Authentication always routes users to
    # their role dashboard, so a caller-supplied next target is unnecessary.
    if request.method == "GET" and "next" in request.GET:
        return redirect("accounts:login")

    show_demo_accounts = getattr(settings, "SHOW_DEMO_ACCOUNTS", True)
    demo_accounts = []
    default_demo_password = ""

    if show_demo_accounts:
        default_demo_password = os.getenv("DEMO_DEFAULT_PASSWORD", "RoomLoop2026!")
        raw_demo_accounts = [
            {
                "role_id": "student",
                "label": "Student",
                "name": "Arun Kumar",
                "email": "arun.kumar@student.roomloop.edu",
                "badge": "Resident Student",
                "description": "Student room dashboard & swap center",
                "icon": "academic-cap",
            },
            {
                "role_id": "warden",
                "label": "Warden",
                "name": "Dr. Alok Sharma",
                "email": "warden.north@roomloop.edu",
                "badge": "Hostel Authority",
                "description": "North residence allocations & approvals",
                "icon": "shield-check",
            },
            {
                "role_id": "chief_warden",
                "label": "Chief Warden",
                "name": "Prof. Ramanathan K.",
                "email": "chief.warden@roomloop.edu",
                "badge": "Campus Authority",
                "description": "Cross-hostel approvals & campus governance",
                "icon": "badge-check",
            },
            {
                "role_id": "hostel_admin",
                "label": "Hostel Administrator",
                "name": "Suresh Verma",
                "email": "hostel.admin@roomloop.edu",
                "badge": "Operations",
                "description": "Residential policy & student onboarding",
                "icon": "building-office",
            },
            {
                "role_id": "dean",
                "label": "Dean",
                "name": "Prof. Aruna Roy",
                "email": "dean@roomloop.edu",
                "badge": "Student Welfare",
                "description": "Institution-wide audit & live analytics",
                "icon": "chart-bar",
            },
            {
                "role_id": "sysadmin",
                "label": "System Administrator",
                "name": "System Administrator",
                "email": "admin@roomloop.edu",
                "badge": "Platform Tech",
                "description": "Role governance & system health",
                "icon": "cog",
            },
        ]

        cache_key = "roomloop_login_demo_active_emails"
        existing_emails_lower = cache.get(cache_key)
        if existing_emails_lower is None:
            existing_emails = set(
                User.objects.filter(
                    email__in=[d["email"] for d in raw_demo_accounts],
                    is_active=True,
                ).values_list("email", flat=True)
            )
            existing_emails_lower = {e.lower() for e in existing_emails}
            cache.set(cache_key, existing_emails_lower, timeout=300)
        for item in raw_demo_accounts:
            item["is_available"] = item["email"].lower() in existing_emails_lower
            item["password"] = default_demo_password
            demo_accounts.append(item)

    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            user = form.user
            login(request, user)
            if form.cleaned_data.get("remember_me"):
                request.session.set_expiry(60 * 60 * 24 * 30)  # 30 days
            else:
                request.session.set_expiry(0)  # Expires when browser closes

            # Audit login event
            AuditLog.objects.create(
                acting_user=user,
                institution=user.institution,
                action="USER_LOGIN",
                target_type="User",
                target_id=str(user.id),
                change_summary=f"Successful login for {user.email}",
                ip_address=request.META.get("REMOTE_ADDR"),
            )

            messages.success(request, f"Welcome back, {user.full_name}!")
            return redirect(get_dashboard_url_for_user(user))
    else:
        form = LoginForm()

    context = {
        "form": form,
        "show_demo_accounts": show_demo_accounts,
        "demo_accounts": demo_accounts,
        "default_demo_password": default_demo_password,
    }
    return render(request, "accounts/login.html", context)


def logout_view(request):
    """User logout view."""
    if request.user.is_authenticated:
        AuditLog.objects.create(
            acting_user=request.user,
            institution=request.user.institution,
            action="USER_LOGOUT",
            target_type="User",
            target_id=str(request.user.id),
            change_summary=f"Logged out {request.user.email}",
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        logout(request)
        messages.info(request, "You have been safely signed out.")
    return redirect("accounts:login")


@login_required
def dashboard_router(request):
    """Deterministic router redirecting authenticated users to their specific dashboard."""
    return redirect(get_dashboard_url_for_user(request.user))


@login_required
def profile_view(request):
    """View and update profile information."""
    user = request.user
    # Fetch active occupancy if user is a student
    from apps.hostels.selectors import get_student_active_occupancy
    current_occupancy = get_student_active_occupancy(user) if user.is_student_role else None

    if request.method == "POST":
        form = UserProfileForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            update_user_profile(
                user=user,
                full_name=form.cleaned_data["full_name"],
                phone_number=form.cleaned_data["phone_number"],
                avatar=form.cleaned_data.get("avatar"),
                acting_user=user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Your profile has been updated successfully.")
            return redirect("accounts:profile")
    else:
        form = UserProfileForm(instance=user)

    return render(
        request,
        "accounts/profile.html",
        {
            "form": form,
            "current_occupancy": current_occupancy,
            "active_tab": "profile",
            "active_nav": "profile",
        },
    )


@login_required
def password_change_view(request):
    """Self-service password change view."""
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            AuditLog.objects.create(
                acting_user=user,
                institution=user.institution,
                action="PASSWORD_CHANGED",
                target_type="User",
                target_id=str(user.id),
                change_summary="Password successfully updated",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Your password was successfully updated!")
            return redirect("accounts:profile")
    else:
        form = PasswordChangeForm(request.user)

    return render(
        request,
        "accounts/password_change.html",
        {"form": form, "active_tab": "profile"},
    )


# Specific Role Dashboards
@login_required
def student_dashboard_view(request):
    """Student Accommodation Dashboard."""
    from apps.hostels.selectors import get_student_dashboard_context
    from apps.exchange.selectors import get_student_exchange_requests, get_student_proposals
    from apps.exchange.models import ExchangeProposal, ProposalParticipant
    from apps.hostels.models import Hostel

    context = get_student_dashboard_context(request.user)
    context["exchange_requests"] = get_student_exchange_requests(request.user)[:3]
    context["incoming_proposals"] = (
        get_student_proposals(request.user)
        .filter(
            status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
            participants__student=request.user,
            participants__response_status=ProposalParticipant.ResponseStatus.PENDING,
        )
        .distinct()
    )
    context["active_nav"] = "dashboard"

    # Eligible hostels overview for verified students
    if request.user.residential_eligibility in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
        context["eligible_hostels"] = (
            Hostel.objects.filter(
                institution=request.user.institution,
                status=Hostel.Status.ACTIVE,
                resident_category=request.user.residential_eligibility,
            )
            .prefetch_related("blocks")
            .order_by("name")
        )
    else:
        context["eligible_hostels"] = []

    return render(request, "dashboards/student.html", context)


@login_required
def warden_dashboard_view(request):
    """Warden Dashboard scoped strictly to assigned hostels."""
    if not (request.user.is_warden_role or request.user.is_hostel_admin_role):
        raise PermissionDenied("Access restricted to authorized Wardens.")

    from apps.hostels.selectors import get_warden_dashboard_context
    context = get_warden_dashboard_context(request.user)
    context["active_nav"] = "dashboard"
    return render(request, "dashboards/warden.html", context)


@login_required
def chief_warden_dashboard_view(request):
    """Chief Warden Dashboard with institution-wide overview."""
    if not (request.user.is_chief_warden_role or request.user.is_hostel_admin_role):
        raise PermissionDenied("Access restricted to Chief Wardens.")

    from apps.hostels.selectors import get_chief_warden_dashboard_context
    context = get_chief_warden_dashboard_context(request.user)
    context["active_nav"] = "dashboard"
    return render(request, "dashboards/chief_warden.html", context)


@login_required
def hostel_admin_dashboard_view(request):
    """Hostel Administrator Dashboard for full inventory and occupancy control."""
    if request.user.is_sysadmin_role:
        return redirect("accounts:dashboard_sysadmin")
    if not (request.user.is_hostel_admin_role or request.user.is_superuser):
        raise PermissionDenied("Access restricted to Hostel Administrators.")

    from apps.hostels.selectors import get_admin_dashboard_context
    context = get_admin_dashboard_context(request.user)
    context["pending_eligibility_count"] = get_pending_eligibility_count(request.user.institution)
    context["active_nav"] = "dashboard"
    return render(request, "dashboards/hostel_admin.html", context)


@login_required
def sysadmin_dashboard_view(request):
    """System Administrator Technical Dashboard (Accounts, Security, Audit Logs, Health)."""
    if not (request.user.is_sysadmin_role or request.user.is_superuser):
        raise PermissionDenied("Access restricted to System Administrators.")

    technical_actions = [
        "USER_LOGIN",
        "USER_LOGOUT",
        "PASSWORD_CHANGED",
        "USER_CREATED",
        "PROFILE_UPDATED",
        "STUDENT_CREATED",
        "STUDENT_UPDATED",
        "STUDENT_ACTIVATED",
        "STUDENT_DEACTIVATED",
        "STUDENT_IMPORT_COMPLETED",
        "PASSWORD_RESET_INITIATED",
        "PASSWORD_SET",
    ]
    recent_audit_logs = (
        AuditLog.objects.filter(
            Q(action__in=technical_actions) | Q(target_type__in=["User", "System", "Configuration"])
        )
        .exclude(target_type="StudentOccupancy")
        .select_related("acting_user", "institution")
        .order_by("-timestamp")[:12]
    )
    stats = User.objects.aggregate(
        total_users=Count("id"),
        active_users=Count("id", filter=Q(is_active=True)),
        students=Count("id", filter=Q(groups__name="Student")),
        wardens=Count("id", filter=Q(groups__name="Warden")),
        chief_wardens=Count("id", filter=Q(groups__name="Chief Warden")),
        hostel_admins=Count("id", filter=Q(groups__name="Hostel Administrator")),
        deans=Count("id", filter=Q(groups__name="Dean of Student Welfare")),
        sysadmins=Count("id", filter=Q(groups__name="System Administrator")),
    )
    total_users = stats["total_users"]
    active_users = stats["active_users"]
    roles_summary = [
        {"name": "Students", "count": stats["students"]},
        {"name": "Wardens", "count": stats["wardens"]},
        {"name": "Chief Wardens", "count": stats["chief_wardens"]},
        {"name": "Hostel Admins", "count": stats["hostel_admins"]},
        {"name": "Deans", "count": stats["deans"]},
        {"name": "System Admins", "count": stats["sysadmins"]},
    ]

    context = {
        "active_nav": "dashboard",
        "recent_audit_logs": recent_audit_logs,
        "total_users": total_users,
        "active_users": active_users,
        "roles_summary": roles_summary,
        "db_backend": "PostgreSQL 16 (psycopg)",
        "system_status": "Operational",
    }
    return render(request, "dashboards/sysadmin.html", context)


@login_required
def dean_dashboard_view(request):
    """Dean of Student Welfare Executive & Infrastructure Overview Dashboard."""
    if not (request.user.is_dean_role or request.user.is_superuser):
        raise PermissionDenied("Access restricted to Deans of Student Welfare.")

    from apps.hostels.selectors import get_dean_dashboard_context
    context = get_dean_dashboard_context(request.user)
    context["active_nav"] = "dashboard"
    return render(request, "dashboards/dean.html", context)


@login_required
@require_http_methods(["GET"])
def dashboard_stats_api_view(request):
    """Unified AJAX statistics endpoint returning live metrics across all roles."""
    user = request.user
    role = getattr(user, "primary_role", None) or ("student" if user.is_student_role else "unknown")
    stats = {}

    try:
        if user.is_student_role:
            from apps.exchange.selectors import get_student_exchange_requests, get_student_proposals
            from apps.exchange.models import ExchangeProposal, ProposalParticipant
            from apps.hostels.models import Hostel

            active_reqs = get_student_exchange_requests(user).count()
            incoming_props = (
                get_student_proposals(user)
                .filter(
                    status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
                    participants__student=user,
                    participants__response_status=ProposalParticipant.ResponseStatus.PENDING,
                )
                .distinct()
                .count()
            )
            if user.residential_eligibility in [Hostel.ResidentCategory.MALE, Hostel.ResidentCategory.FEMALE]:
                eligible_hostels_count = Hostel.objects.filter(
                    institution=user.institution,
                    status=Hostel.Status.ACTIVE,
                    resident_category=user.residential_eligibility,
                ).count()
            else:
                eligible_hostels_count = 0

            stats = {
                "available_hostels": eligible_hostels_count,
                "eligible_category": user.claimed_residential_category or "Hostel",
                "active_requests": active_reqs,
                "incoming_proposals": incoming_props,
            }
        elif user.is_warden_role:
            from apps.hostels.selectors import get_warden_dashboard_context
            ctx = get_warden_dashboard_context(user)
            stats = {
                "vacant_beds": ctx.get("vacant_beds", 0),
                "pending_reviews_count": ctx.get("pending_reviews_count", 0),
                "occupied_beds": ctx.get("occupied_beds", 0),
                "occupancy_pct": ctx.get("occupancy_pct", 0),
                "total_beds": ctx.get("total_beds", 0),
                "total_rooms": ctx.get("total_rooms", 0),
            }
        elif user.is_chief_warden_role:
            from apps.hostels.selectors import get_chief_warden_dashboard_context
            ctx = get_chief_warden_dashboard_context(user)
            stats = {
                "total_hostels": ctx.get("total_hostels", 0),
                "total_beds": ctx.get("total_beds", 0),
                "occupied_beds": ctx.get("occupied_beds", 0),
                "available_beds": ctx.get("available_beds", 0),
                "occupancy_pct": ctx.get("occupancy_pct", 0),
                "cross_hostel_requests_count": ctx.get("cross_hostel_requests_count", 0),
            }
        elif user.is_hostel_admin_role:
            from apps.hostels.selectors import get_admin_dashboard_context
            ctx = get_admin_dashboard_context(user)
            pending_elig = get_pending_eligibility_count(user.institution)
            stats = {
                "total_hostels": ctx.get("total_hostels", 0),
                "total_rooms": ctx.get("total_rooms", 0),
                "total_beds": ctx.get("total_beds", 0),
                "occupied_beds": ctx.get("occupied_beds", 0),
                "available_beds": ctx.get("available_beds", 0),
                "occupancy_pct": ctx.get("occupancy_pct", 0),
                "pending_eligibility_count": pending_elig,
            }
        elif user.is_dean_role:
            from apps.hostels.selectors import get_dean_dashboard_context
            ctx = get_dean_dashboard_context(user)
            stats = {
                "total_hostels": ctx.get("total_hostels", 0),
                "total_rooms": ctx.get("total_rooms", 0),
                "total_beds": ctx.get("total_beds", 0),
                "occupied_beds": ctx.get("occupied_beds", 0),
                "available_beds": ctx.get("available_beds", 0),
                "occupancy_pct": ctx.get("occupancy_pct", 0),
            }
        elif user.is_sysadmin_role or user.is_superuser:
            user_stats = User.objects.aggregate(
                total_users=Count("id"),
                active_users=Count("id", filter=Q(is_active=True)),
            )
            stats = {
                "total_users": user_stats["total_users"],
                "active_users": user_stats["active_users"],
                "system_status": "Operational",
            }

        return JsonResponse({
            "status": "success",
            "role": role,
            "stats": stats,
            "timestamp": "Just now",
        })
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e),
        }, status=500)


# ==============================================================================
# System Administrator: Student Management Views
# ==============================================================================

@login_required
def student_list_view(request):
    """Directory of student accounts for the System Administrator."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    institution = request.user.institution
    search_query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()
    eligibility_filter = request.GET.get("eligibility", "").strip()

    qs = get_student_directory_queryset(
        institution=institution,
        search_query=search_query,
        account_status=status_filter,
        eligibility=eligibility_filter,
    )

    paginator = Paginator(qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Summary metrics in a single aggregated query
    stats = User.objects.filter(institution=institution, groups__name=ROLE_STUDENT).aggregate(
        total_count=Count("id", distinct=True),
        active_count=Count("id", filter=Q(is_active=True), distinct=True),
        unverified_count=Count(
            "id",
            filter=Q(residential_eligibility=User.ResidentialEligibility.UNVERIFIED),
            distinct=True,
        ),
    )
    total_count = stats["total_count"]
    active_count = stats["active_count"]
    unverified_count = stats["unverified_count"]

    is_filtered = bool(search_query or status_filter or eligibility_filter)

    context = {
        "page_obj": page_obj,
        "search_query": search_query,
        "status_filter": status_filter,
        "eligibility_filter": eligibility_filter,
        "total_count": total_count,
        "active_count": active_count,
        "unverified_count": unverified_count,
        "results_count": paginator.count,
        "is_filtered": is_filtered,
        "active_nav": "students_directory",
    }
    return render(request, "accounts/students/student_list.html", context)


@login_required
def student_create_view(request):
    """System Administrator single student account creation view."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    institution = request.user.institution
    if request.method == "POST":
        form = StudentCreateForm(request.POST, institution=institution)
        if form.is_valid():
            try:
                student = create_student_account(
                    acting_user=request.user,
                    full_name=form.cleaned_data["full_name"],
                    registration_number=form.cleaned_data["registration_number"],
                    email=form.cleaned_data["email"],
                    department=form.cleaned_data.get("department", ""),
                    program=form.cleaned_data.get("program", ""),
                    academic_year=form.cleaned_data.get("academic_year", ""),
                    phone_number=form.cleaned_data.get("phone_number", ""),
                    claimed_residential_category=form.cleaned_data.get(
                        "claimed_residential_category", User.ClaimedResidentialCategory.UNSPECIFIED
                    ),
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                initiate_student_password_setup(acting_user=request.user, student=student, request=request)
                messages.success(
                    request,
                    f"Student account for {student.full_name} ({student.registration_number}) created successfully with default password 'RoomLoop2026!'. "
                    "First-time password setup instructions have been sent to their institutional email.",
                )
                return redirect("accounts:student_detail", pk=student.id)
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = StudentCreateForm(institution=institution)

    return render(
        request,
        "accounts/students/student_form.html",
        {"form": form, "active_nav": "students_add", "is_create": True},
    )


@login_required
def student_detail_view(request, pk):
    """Comprehensive student account detail view with read-only occupancy and audit history."""
    if not (can_manage_student_accounts(request.user) or can_verify_residential_eligibility(request.user)):
        raise PermissionDenied("Access restricted to authorized administrators.")

    institution = request.user.institution
    student = get_object_or_404(User, pk=pk, institution=institution, groups__name=ROLE_STUDENT)
    context = get_student_detail_context(student, request.user)
    context["active_nav"] = "students_directory" if can_manage_student_accounts(request.user) else "eligibility_verification"
    return render(request, "accounts/students/student_detail.html", context)


@login_required
def student_edit_view(request, pk):
    """System Administrator student account editing view."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    institution = request.user.institution
    student = get_object_or_404(User, pk=pk, institution=institution, groups__name=ROLE_STUDENT)

    if request.method == "POST":
        form = StudentEditForm(request.POST, instance=student, institution=institution)
        if form.is_valid():
            try:
                update_student_account(
                    acting_user=request.user,
                    student=student,
                    full_name=form.cleaned_data["full_name"],
                    registration_number=form.cleaned_data["registration_number"],
                    department=form.cleaned_data.get("department", ""),
                    program=form.cleaned_data.get("program", ""),
                    academic_year=form.cleaned_data.get("academic_year", ""),
                    phone_number=form.cleaned_data.get("phone_number", ""),
                    claimed_residential_category=form.cleaned_data.get("claimed_residential_category"),
                    change_reason=form.cleaned_data.get("change_reason", ""),
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, f"Student account details for {student.full_name} updated successfully.")
                return redirect("accounts:student_detail", pk=student.id)
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = StudentEditForm(instance=student, institution=institution)

    return render(
        request,
        "accounts/students/student_edit.html",
        {"form": form, "student": student, "active_nav": "students_directory"},
    )


@login_required
def student_toggle_status_view(request, pk):
    """View to activate or deactivate a student account with occupancy warnings."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    institution = request.user.institution
    student = get_object_or_404(User, pk=pk, institution=institution, groups__name=ROLE_STUDENT)
    is_deactivation = student.is_active
    current_occupancy = student.current_occupancy

    if request.method == "POST":
        form = StudentStatusToggleForm(request.POST, is_deactivation=is_deactivation)
        if form.is_valid():
            new_status = not student.is_active
            reason = form.cleaned_data.get("reason", "")
            try:
                student, warning_msg = set_student_account_status(
                    acting_user=request.user,
                    student=student,
                    is_active=new_status,
                    reason=reason,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                if new_status:
                    messages.success(request, f"Student account for {student.full_name} has been activated.")
                else:
                    if warning_msg:
                        messages.warning(request, warning_msg)
                    messages.success(request, f"Student account for {student.full_name} has been deactivated.")
                return redirect("accounts:student_detail", pk=student.id)
            except ValidationError as e:
                messages.error(request, str(e))
    else:
        form = StudentStatusToggleForm(is_deactivation=is_deactivation)

    return render(
        request,
        "accounts/students/student_confirm_deactivate.html",
        {
            "form": form,
            "student": student,
            "is_deactivation": is_deactivation,
            "current_occupancy": current_occupancy,
            "active_nav": "students_directory",
        },
    )


@login_required
@require_http_methods(["POST"])
def student_password_reset_initiate_view(request, pk):
    """System Administrator view to trigger password setup / reset email."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    institution = request.user.institution
    student = get_object_or_404(User, pk=pk, institution=institution, groups__name=ROLE_STUDENT)

    initiate_student_password_setup(acting_user=request.user, student=student, request=request)
    messages.success(
        request,
        f"Password setup instructions have been sent to {student.email}."
    )
    return redirect("accounts:student_detail", pk=student.id)


@login_required
def student_csv_template_download_view(request):
    """Download sample CSV roster template for System Administrators."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="roomloop_student_roster_template.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "full_name",
        "registration_number",
        "email",
        "department",
        "program",
        "academic_year",
        "phone_number",
        "claimed_residential_category",
    ])
    writer.writerow([
        "Aarav Sharma",
        "2024CS101",
        "aarav.sharma@apex.edu",
        "Computer Science & Engineering",
        "B.Tech",
        "first_year",
        "+91 9876543210",
        "male",
    ])
    writer.writerow([
        "Priya Patel",
        "2024EE205",
        "priya.patel@apex.edu",
        "Electrical Engineering",
        "B.Tech",
        "first_year",
        "+91 9876543211",
        "female",
    ])
    return response


@login_required
def student_csv_import_view(request):
    """Two-stage CSV import workflow with rigorous preview and confirmation."""
    if not can_manage_student_accounts(request.user):
        raise PermissionDenied("Access restricted to System Administrators.")

    institution = request.user.institution
    form = StudentCSVUploadForm()
    preview_data = None

    if request.method == "POST":
        if "confirm_import" in request.POST:
            session_rows = request.session.get("csv_import_valid_rows", [])
            if not session_rows:
                messages.error(request, "No valid import data found in session. Please upload your CSV file again.")
                return redirect("accounts:student_import")

            created_count, error_list = process_csv_student_import(
                acting_user=request.user,
                rows=session_rows,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            # Clear session keys
            request.session.pop("csv_import_valid_rows", None)
            request.session.pop("csv_import_preview_summary", None)

            messages.success(
                request,
                f"CSV Import Completed: {created_count} student accounts created successfully with default password 'RoomLoop2026!'."
            )
            if error_list:
                messages.warning(
                    request,
                    f"{len(error_list)} rows were skipped due to conflicts or errors."
                )
            return redirect("accounts:student_list")

        elif "cancel_import" in request.POST:
            request.session.pop("csv_import_valid_rows", None)
            request.session.pop("csv_import_preview_summary", None)
            messages.info(request, "CSV import cancelled.")
            return redirect("accounts:student_import")

        else:
            # File Upload & Validation Preview
            form = StudentCSVUploadForm(request.POST, request.FILES)
            if form.is_valid():
                file = form.cleaned_data["csv_file"]
                try:
                    file_content = file.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    form.add_error("csv_file", "The uploaded file is not valid UTF-8 text.")
                    return render(request, "accounts/students/student_import.html", {"form": form, "active_nav": "students_import"})

                try:
                    validation_summary = validate_csv_student_import(
                        acting_user=request.user,
                        file_obj=file,
                    )
                    preview_data = validation_summary
                    request.session["csv_import_valid_rows"] = validation_summary["valid_rows"]
                    request.session["csv_import_preview_summary"] = {
                        "total_rows": validation_summary["total_rows"],
                        "valid_count": validation_summary["valid_count"],
                        "duplicate_count": validation_summary["duplicate_count"],
                        "error_count": validation_summary["error_count"],
                    }
                except ValidationError as e:
                    form.add_error("csv_file", e)

    return render(
        request,
        "accounts/students/student_import.html",
        {
            "form": form,
            "preview_data": preview_data,
            "active_nav": "students_import",
        },
    )


# ==============================================================================
# Public: First-time Password Setup View
# ==============================================================================

def password_setup_view(request, uidb64, token):
    """Public view for newly created students to set their password for the first time."""
    try:
        uid = urlsafe_base64_decode(uidb64).decode()
        student = User.objects.filter(pk=uid).first()
    except (TypeError, ValueError, OverflowError):
        student = None

    if student is None or not default_token_generator.check_token(student, token):
        return render(
            request,
            "accounts/students/password_setup_invalid.html",
            {"error_message": "This password setup link is invalid or has expired."},
        )

    if request.method == "POST":
        form = PasswordSetupForm(student, request.POST)
        if form.is_valid():
            try:
                complete_student_password_setup(
                    student=student,
                    new_password=form.cleaned_data["new_password1"],
                    token=token,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(
                    request,
                    "Your password has been configured successfully! You may now sign in.",
                )
                return redirect("accounts:login")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = PasswordSetupForm(student)

    return render(
        request,
        "accounts/students/password_setup.html",
        {"form": form, "student": student},
    )


# ==============================================================================
# Hostel Administrator: Residential Eligibility Verification Views
# ==============================================================================

@login_required
def eligibility_verification_list_view(request):
    """Verification queue for Hostel Administrators to review and verify residential eligibility."""
    if not can_verify_residential_eligibility(request.user):
        raise PermissionDenied("Access restricted to Hostel Administrators.")

    institution = request.user.institution
    search_query = request.GET.get("q", "").strip()
    category_filter = request.GET.get("category", "").strip()
    dept_filter = request.GET.get("department", "").strip()
    year_filter = request.GET.get("year", "").strip()

    qs = get_pending_eligibility_students_queryset(
        institution=institution,
        search_query=search_query,
        claimed_category=category_filter,
        department=dept_filter,
        academic_year=year_filter,
    )

    paginator = Paginator(qs, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    pending_count = get_pending_eligibility_count(institution)

    context = {
        "page_obj": page_obj,
        "search_query": search_query,
        "category_filter": category_filter,
        "dept_filter": dept_filter,
        "year_filter": year_filter,
        "pending_count": pending_count,
        "active_nav": "eligibility_verification",
    }
    return render(request, "accounts/eligibility/verification_list.html", context)


@login_required
def eligibility_verify_student_view(request, pk):
    """Hostel Administrator review and verification action view for a specific student."""
    if not can_verify_residential_eligibility(request.user):
        raise PermissionDenied("Access restricted to Hostel Administrators.")

    institution = request.user.institution
    student = get_object_or_404(User, pk=pk, institution=institution, groups__name=ROLE_STUDENT)
    student_context = get_student_detail_context(student, request.user)

    initial_eligibility = student.residential_eligibility
    if initial_eligibility == User.ResidentialEligibility.UNVERIFIED:
        if student.claimed_residential_category in [User.ResidentialEligibility.MALE, User.ResidentialEligibility.FEMALE]:
            initial_eligibility = student.claimed_residential_category
        else:
            initial_eligibility = User.ResidentialEligibility.MALE

    if request.method == "POST":
        form = EligibilityVerificationForm(request.POST)
        if form.is_valid():
            try:
                student, record = verify_student_eligibility(
                    acting_user=request.user,
                    student=student,
                    new_eligibility=form.cleaned_data["new_eligibility"],
                    reason=form.cleaned_data["reason"],
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(
                    request,
                    f"Residential eligibility for {student.full_name} ({student.registration_number}) verified as '{student.get_residential_eligibility_display()}'.",
                )
                return redirect("accounts:eligibility_verification_list")
            except ValidationError as e:
                form.add_error(None, e)
    else:
        form = EligibilityVerificationForm(initial={"new_eligibility": initial_eligibility})

    context = {
        **student_context,
        "form": form,
        "active_nav": "eligibility_verification",
    }
    return render(request, "accounts/eligibility/verify_student.html", context)
