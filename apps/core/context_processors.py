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

    # Check superuser first
    if user.is_superuser:
        context["user_role"] = "System Administrator"
        context["user_role_label"] = "System Admin"
        context["is_sysadmin"] = True
        context["can_manage_inventory"] = True
        context["can_assign_occupancy"] = True
        return context

    groups = set(user.groups.values_list("name", flat=True))

    # Deterministic priority resolution if user has multiple groups:
    # 1. System Administrator
    # 2. Hostel Administrator
    # 3. Chief Warden
    # 4. Warden
    # 5. Dean of Student Welfare
    # 6. Student
    if user.is_superuser or "System Administrator" in groups:
        context["user_role"] = "System Administrator"
        context["user_role_label"] = "System Admin"
        context["is_sysadmin"] = True
        context["can_manage_inventory"] = True
        context["can_assign_occupancy"] = True
    elif "Hostel Administrator" in groups:
        context["user_role"] = "Hostel Administrator"
        context["user_role_label"] = "Hostel Admin"
        context["is_hostel_admin"] = True
        context["can_manage_inventory"] = True
        context["can_assign_occupancy"] = True
    elif "Chief Warden" in groups:
        context["user_role"] = "Chief Warden"
        context["user_role_label"] = "Chief Warden"
        context["is_chief_warden"] = True
        context["can_manage_inventory"] = True
        context["can_assign_occupancy"] = True
    elif "Warden" in groups:
        context["user_role"] = "Warden"
        context["user_role_label"] = "Warden"
        context["is_warden"] = True
        context["can_manage_inventory"] = False  # Wardens view and manage occupancy in assigned hostels
        context["can_assign_occupancy"] = True
    elif "Dean of Student Welfare" in groups:
        context["user_role"] = "Dean of Student Welfare"
        context["user_role_label"] = "Dean"
        context["is_dean"] = True
        context["can_manage_inventory"] = False
        context["can_assign_occupancy"] = False
    elif "Student" in groups:
        context["user_role"] = "Student"
        context["user_role_label"] = "Student"
        context["is_student"] = True
    else:
        # Fallback if no group assigned
        context["user_role"] = "Student"
        context["user_role_label"] = "Student"
        context["is_student"] = True

    return context
