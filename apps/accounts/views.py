from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.db.models import Q

from .forms import LoginForm, UserProfileForm
from .models import User
from .selectors import get_dashboard_url_for_user
from .services import update_user_profile
from apps.core.models import AuditLog


def login_view(request):
    """User login view supporting session persistence and role redirection."""
    if request.user.is_authenticated:
        return redirect(get_dashboard_url_for_user(request.user))

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
            next_url = request.POST.get("next") or request.GET.get("next")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(get_dashboard_url_for_user(user))
    else:
        form = LoginForm()

    return render(request, "accounts/login.html", {"form": form})


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
    context = get_student_dashboard_context(request.user)
    context["active_nav"] = "dashboard"
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
    context["active_nav"] = "dashboard"
    return render(request, "dashboards/hostel_admin.html", context)


@login_required
def sysadmin_dashboard_view(request):
    """System Administrator Technical Dashboard (Accounts, Security, Audit Logs, Health)."""
    if not (request.user.is_sysadmin_role or request.user.is_superuser):
        raise PermissionDenied("Access restricted to System Administrators.")

    from apps.core.models import AuditLog
    # Restrict audit stream strictly to technical, authentication, and provisioning events.
    # Exclude operational student occupancy allocations, releases, and welfare payloads.
    technical_actions = [
        "USER_LOGIN",
        "USER_LOGOUT",
        "PASSWORD_CHANGED",
        "USER_CREATED",
        "PROFILE_UPDATED",
    ]
    recent_audit_logs = (
        AuditLog.objects.filter(
            Q(action__in=technical_actions) | Q(target_type__in=["User", "System", "Configuration"])
        )
        .exclude(target_type="StudentOccupancy")
        .select_related("acting_user", "institution")
        .order_by("-timestamp")[:12]
    )
    total_users = User.objects.count()
    active_users = User.objects.filter(is_active=True).count()
    roles_summary = [
        {"name": "Students", "count": User.objects.filter(groups__name="Student").count()},
        {"name": "Wardens", "count": User.objects.filter(groups__name="Warden").count()},
        {"name": "Chief Wardens", "count": User.objects.filter(groups__name="Chief Warden").count()},
        {"name": "Hostel Admins", "count": User.objects.filter(groups__name="Hostel Administrator").count()},
        {"name": "Deans", "count": User.objects.filter(groups__name="Dean of Student Welfare").count()},
        {"name": "System Admins", "count": User.objects.filter(groups__name="System Administrator").count()},
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
