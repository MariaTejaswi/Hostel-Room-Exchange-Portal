from django.urls import reverse
from django.db.models import Q
from .models import User, EligibilityVerificationRecord
from .services import (
    ROLE_STUDENT,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_HOSTEL_ADMIN,
    ROLE_DEAN,
    ROLE_SYSADMIN,
)
from apps.core.models import AuditLog


def get_dashboard_url_for_user(user: User) -> str:
    """Return the appropriate dashboard URL based on deterministic role evaluation."""
    if not user.is_authenticated:
        return reverse("accounts:login")

    role = user.primary_role

    if role == ROLE_SYSADMIN:
        return reverse("accounts:dashboard_sysadmin")
    elif role == ROLE_HOSTEL_ADMIN:
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


def get_student_directory_queryset(institution, search_query=None, account_status=None, eligibility=None):
    """
    Return queryset of student accounts for the System Administrator directory,
    scoped strictly to the given institution.
    """
    qs = (
        User.objects.filter(
            institution=institution,
            groups__name=ROLE_STUDENT,
        )
        .select_related("institution")
        .prefetch_related(
            "occupancy_records__bed__room__floor__block__hostel",
            "eligibility_verifications__verifier",
        )
        .distinct()
        .order_by("-date_joined")
    )

    if search_query:
        query = search_query.strip()
        qs = qs.filter(
            Q(full_name__icontains=query)
            | Q(registration_number__icontains=query)
            | Q(email__icontains=query)
        )

    if account_status:
        status_val = account_status.strip().lower()
        if status_val == "active":
            qs = qs.filter(is_active=True)
        elif status_val == "inactive":
            qs = qs.filter(is_active=False)

    if eligibility:
        eligibility_val = eligibility.strip().lower()
        if eligibility_val in [choice[0] for choice in User.ResidentialEligibility.choices]:
            qs = qs.filter(residential_eligibility=eligibility_val)

    return qs


def get_pending_eligibility_students_queryset(
    institution,
    search_query=None,
    claimed_category=None,
    department=None,
    academic_year=None,
):
    """
    Return queryset of active students awaiting residential eligibility verification,
    scoped to the given institution.
    """
    qs = (
        User.objects.filter(
            institution=institution,
            groups__name=ROLE_STUDENT,
            residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
            is_active=True,
        )
        .select_related("institution")
        .distinct()
        .order_by("date_joined")
    )

    if search_query:
        query = search_query.strip()
        qs = qs.filter(
            Q(full_name__icontains=query)
            | Q(registration_number__icontains=query)
            | Q(email__icontains=query)
        )

    if claimed_category:
        category_val = claimed_category.strip().lower()
        if category_val in [choice[0] for choice in User.ClaimedResidentialCategory.choices]:
            qs = qs.filter(claimed_residential_category=category_val)

    if department:
        dept_val = department.strip()
        if dept_val:
            qs = qs.filter(department__icontains=dept_val)

    if academic_year:
        year_val = academic_year.strip()
        if year_val:
            qs = qs.filter(academic_year=year_val)

    return qs


def get_pending_eligibility_count(institution) -> int:
    """Return count of active students awaiting residential eligibility verification."""
    if not institution:
        return 0
    return User.objects.filter(
        institution=institution,
        groups__name=ROLE_STUDENT,
        residential_eligibility=User.ResidentialEligibility.UNVERIFIED,
        is_active=True,
    ).distinct().count()


def get_student_detail_context(student: User, actor: User) -> dict:
    """Compile comprehensive context for student account detail view."""
    # Current active occupancy
    current_occupancy = student.current_occupancy

    # Occupancy history
    occupancy_history = (
        student.occupancy_records.all()
        .select_related("bed__room__floor__block__hostel", "created_by")
        .order_by("-start_date")
    )

    # Verification records
    verification_records = (
        student.eligibility_verifications.all()
        .select_related("verifier")
        .order_by("-created_at")
    )

    # Audit events regarding this student
    audit_logs = (
        AuditLog.objects.filter(
            institution=student.institution,
            target_type="User",
            target_id=str(student.id),
        )
        .select_related("acting_user")
        .order_by("-timestamp")[:25]
    )

    # Exchange requests (if exchange app is active)
    try:
        from apps.exchange.models import ExchangeRequest
        exchange_requests = (
            ExchangeRequest.objects.filter(student=student)
            .select_related("source_bed__room__floor__block__hostel")
            .order_by("-submitted_at")[:10]
        )
    except Exception:
        exchange_requests = []

    return {
        "student": student,
        "current_occupancy": current_occupancy,
        "occupancy_history": occupancy_history,
        "verification_records": verification_records,
        "audit_logs": audit_logs,
        "exchange_requests": exchange_requests,
        "has_active_occupancy": current_occupancy is not None,
    }
