from django.urls import reverse
from .models import User
from .services import (
    ROLE_STUDENT,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_HOSTEL_ADMIN,
    ROLE_DEAN,
    ROLE_SYSADMIN,
)


def get_dashboard_url_for_user(user: User) -> str:
    """Return the appropriate dashboard URL based on deterministic role evaluation."""
    if not user.is_authenticated:
        return reverse("accounts:login")

    role = user.primary_role

    if role in (ROLE_SYSADMIN, ROLE_HOSTEL_ADMIN):
        return reverse("accounts:dashboard_admin")
    elif role == ROLE_CHIEF_WARDEN:
        return reverse("accounts:dashboard_chief_warden")
    elif role == ROLE_WARDEN:
        return reverse("accounts:dashboard_warden")
    elif role == ROLE_DEAN:
        return reverse("accounts:dashboard_dean")
    else:
        return reverse("accounts:dashboard_student")


def get_users_for_institution(institution, role_name=None):
    """Return users scoped to a specific institution, optionally filtered by role."""
    qs = User.objects.filter(institution=institution, is_active=True).select_related("institution")
    if role_name:
        qs = qs.filter(groups__name=role_name)
    return qs
