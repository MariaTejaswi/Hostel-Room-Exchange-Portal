def navigation_context(request):
    """
    Context processor to inject current role, institution details, and navigation
    context into all templates uniformly.
    """
    context = {
        "user_role": None,
        "user_role_label": None,
        "is_student": False,
        "is_warden": False,
        "is_chief_warden": False,
        "is_hostel_admin": False,
        "is_dean": False,
        "is_sysadmin": False,
        "can_manage_inventory": False,
        "can_assign_occupancy": False,
        "current_institution": None,
        "app_name": "RoomLoop",
        "app_version": "1.0.0 (Phase 1)",
    }

    if not request.user.is_authenticated:
        return context

    user = request.user
    context["current_institution"] = getattr(user, "institution", None)

    # Resolve context using authoritative model primary_role in single evaluation
    role = user.primary_role
    if role == "Dean of Student Welfare":
        context["user_role"] = "Dean of Student Welfare"
        context["user_role_label"] = "Dean"
        context["is_dean"] = True
        context["can_manage_inventory"] = True   # Full infrastructure authority (hostels, rooms, beds)
        context["can_assign_occupancy"] = False  # Operational boundary: cannot directly allocate students
    elif role == "Hostel Administrator":
        context["user_role"] = "Hostel Administrator"
        context["user_role_label"] = "Hostel Admin"
        context["is_hostel_admin"] = True
        context["can_manage_inventory"] = True   # Infrastructure management authority
        context["can_assign_occupancy"] = False  # Denied routine allocations; emergency workflow deferred
        if getattr(user, "institution", None):
            from apps.accounts.selectors import get_pending_eligibility_count
            context["pending_eligibility_count"] = get_pending_eligibility_count(user.institution)
    elif role == "Chief Warden":
        context["user_role"] = "Chief Warden"
        context["user_role_label"] = "Chief Warden"
        context["is_chief_warden"] = True
        context["can_manage_inventory"] = False  # Oversight & escalation only; no infrastructure mutation
        context["can_assign_occupancy"] = False  # Routine allocations belong to destination wardens
    elif role == "Warden":
        from apps.hostels.selectors import get_warden_assigned_hostel_ids
        assigned_ids = get_warden_assigned_hostel_ids(user)
        has_assignments = bool(assigned_ids)
        context["user_role"] = "Warden"
        context["user_role_label"] = "Warden"
        context["is_warden"] = True
        context["can_manage_inventory"] = False  # Routine operations in assigned hostels
        context["can_assign_occupancy"] = has_assignments  # Authorized only when assigned to at least one hostel
        context["has_assigned_hostels"] = has_assignments
    elif role == "System Administrator":
        context["user_role"] = "System Administrator"
        context["user_role_label"] = "System Admin"
        context["is_sysadmin"] = True
        context["can_manage_inventory"] = False  # Technical administration only; no operational authority
        context["can_assign_occupancy"] = False  # Cannot allocate beds
    else:
        context["user_role"] = "Student"
        context["user_role_label"] = "Student"
        context["is_student"] = True
        context["can_manage_inventory"] = False
        context["can_assign_occupancy"] = False

    if context.get("is_warden") or context.get("is_chief_warden") or context.get("is_dean"):
        from apps.exchange.selectors import get_pending_approvals_count
        context["pending_approval_count"] = get_pending_approvals_count(user)

    # Universal notifications context across all user profiles
    try:
        from apps.exchange.models import Notification
        recent = list(
            Notification.objects.filter(recipient=user)
            .select_related("exchange_request", "proposal")
            .order_by("-created_at")[:5]
        )
        context["recent_notifications"] = recent
        if len(recent) < 5:
            # If user has fewer than 5 notifications total, count unread from the loaded slice directly
            context["unread_notifications_count"] = sum(1 for n in recent if n.read_at is None)
        else:
            context["unread_notifications_count"] = Notification.objects.filter(
                recipient=user, read_at__isnull=True
            ).count()
    except Exception:
        context["unread_notifications_count"] = 0
        context["recent_notifications"] = []

    return context
