import pytest
from django.test import Client
from django.urls import reverse
from apps.accounts.models import User
from apps.accounts.services import create_user_with_role, ROLE_WARDEN
from apps.hostels.models import StudentOccupancy, Hostel


@pytest.mark.django_db
def test_favicon_route(client):
    """Verify /favicon.ico route does not return 404."""
    response = client.get("/favicon.ico")
    assert response.status_code in (200, 301, 302)
    if response.status_code in (301, 302):
        assert "logo.svg" in response.url


@pytest.mark.django_db
def test_student_allocated_login_and_dashboard(client, student_user, hostel_setup):
    """Verify allocated student can login with CSRF enforcement and view their room allocation."""
    # Allocate student to bed
    StudentOccupancy.objects.create(
        student=student_user,
        bed=hostel_setup["bed_a"],
        institution=student_user.institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        source=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
    )

    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": student_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_student")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "North Wing" in dash_resp.content.decode()
    assert "Active Allocation" in dash_resp.content.decode()


@pytest.mark.django_db
def test_student_unallocated_empty_state(client, student_user):
    """Verify unallocated student sees the polished empty state."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": student_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "No Accommodation Currently Allocated" in dash_resp.content.decode()


@pytest.mark.django_db
def test_warden_assigned_dashboard_flow(client, warden_user, hostel_setup):
    """Verify warden with assigned hostel accesses console and metrics."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": warden_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_warden")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "Warden Operations Console" in dash_resp.content.decode()
    assert "North Wing" in dash_resp.content.decode()


@pytest.mark.django_db
def test_warden_unassigned_empty_state(client, institution, roles):
    """Verify warden without assigned hostels sees clear empty state."""
    unassigned_warden = create_user_with_role(
        email="unassigned.w@apex.test",
        full_name="Unassigned Warden",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": unassigned_warden.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "You have not yet been assigned to supervise any hostels." in dash_resp.content.decode()


@pytest.mark.django_db
def test_chief_warden_dashboard_flow(client, chief_warden_user, hostel_setup):
    """Verify chief warden accesses institution overview."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": chief_warden_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_chief_warden")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "Institution Hostel Overview" in dash_resp.content.decode()


@pytest.mark.django_db
def test_hostel_admin_dashboard_flow(client, admin_user, hostel_setup):
    """Verify hostel admin accesses administrative dashboard."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": admin_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_admin")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "Administrative Metric Cards" in dash_resp.content.decode()


@pytest.mark.django_db
def test_sysadmin_dashboard_flow(client, sysadmin_user, hostel_setup):
    """Verify system administrator accesses technical administration dashboard."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": sysadmin_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_sysadmin")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "Technical Administration Console" in dash_resp.content.decode()


@pytest.mark.django_db
def test_dean_dashboard_flow(client, dean_user, hostel_setup):
    """Verify dean accesses executive console with infrastructure management."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": dean_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_dean")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200
    assert "Dean of Student Welfare Console" in dash_resp.content.decode()
    assert "North Wing" in dash_resp.content.decode()


@pytest.mark.django_db
def test_logout_and_relogin(client, student_user):
    """Verify logout terminates session and subsequent login works."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    csrf_client.post(
        reverse("accounts:login"),
        {"email": student_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    # Logout
    logout_resp = csrf_client.get(reverse("accounts:logout"))
    assert logout_resp.status_code == 302
    assert reverse("accounts:login") in logout_resp.url

    # Check protected page is blocked
    check_resp = csrf_client.get(reverse("accounts:dashboard_student"))
    assert check_resp.status_code == 302
    assert reverse("accounts:login") in check_resp.url

    # Relogin
    login_get2 = csrf_client.get(reverse("accounts:login"))
    token2 = login_get2.cookies["csrftoken"].value
    relogin_resp = csrf_client.post(
        reverse("accounts:login"),
        {"email": student_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token2},
    )
    assert relogin_resp.status_code == 302
    assert relogin_resp.url == reverse("accounts:dashboard_student")


@pytest.mark.django_db
def test_rbac_dashboard_restrictions(client, student_user, warden_user):
    """Verify students and wardens cannot access unauthorized dashboards."""
    # Student cannot access admin, sysadmin, warden, or chief-warden dashboard
    client.force_login(student_user)
    assert client.get(reverse("accounts:dashboard_admin")).status_code == 403
    assert client.get(reverse("accounts:dashboard_sysadmin")).status_code == 403
    assert client.get(reverse("accounts:dashboard_warden")).status_code == 403
    assert client.get(reverse("accounts:dashboard_chief_warden")).status_code == 403

    # Warden cannot access admin or sysadmin dashboard
    client.force_login(warden_user)
    assert client.get(reverse("accounts:dashboard_admin")).status_code == 403
    assert client.get(reverse("accounts:dashboard_sysadmin")).status_code == 403


@pytest.mark.django_db
def test_dean_infrastructure_authority_and_allocation_restriction(client, dean_user, hostel_setup):
    """Verify Dean has infrastructure management authority but is blocked from bed allocation."""
    client.force_login(dean_user)
    
    # Dean CAN access infrastructure management views
    assert client.get(reverse("hostels:hostel_create")).status_code == 200
    assert client.get(reverse("hostels:hostel_edit", kwargs={"pk": hostel_setup["hostel"].id})).status_code == 200
    assert client.get(reverse("hostels:room_create")).status_code == 200
    
    # Dean CANNOT perform direct operational student allocations
    assert client.get(reverse("hostels:occupancy_assign")).status_code == 403


@pytest.mark.django_db
def test_sysadmin_allocation_and_infrastructure_restriction(client, sysadmin_user, hostel_setup):
    """Verify System Administrator is strictly restricted from business operations and allocations."""
    client.force_login(sysadmin_user)
    
    # SysAdmin CANNOT access operational student allocations
    assert client.get(reverse("hostels:occupancy_assign")).status_code == 403
    
    # SysAdmin CANNOT access infrastructure creation (reserved for Dean/Hostel Admin)
    assert client.get(reverse("hostels:hostel_create")).status_code == 403


@pytest.mark.django_db
def test_warden_allocation_limited_to_assigned_hostel(client, warden_user, other_institution, roles, hostel_setup):
    """Verify warden cannot allocate beds in unassigned hostels."""
    from apps.hostels.models import Hostel, Block, Floor, Room, Bed
    # Create an unassigned hostel in same institution
    unassigned_hostel = Hostel.objects.create(
        institution=warden_user.institution,
        name="Unassigned Hall",
        code="UH-99",
    )
    b = Block.objects.create(hostel=unassigned_hostel, name="Block U", code="BU")
    fl = Floor.objects.create(block=b, floor_number=1, floor_label="1st")
    rm = Room.objects.create(floor=fl, room_number="901", room_type="single")
    unassigned_bed = Bed.objects.create(room=rm, bed_label="Bed U1")

    # In assign form, unassigned bed must not be in warden's form choices
    from apps.hostels.forms import OccupancyAssignForm
    form = OccupancyAssignForm(institution=warden_user.institution, user=warden_user)
    assert unassigned_bed not in form.fields["bed"].queryset
    assert hostel_setup["bed_a"] in form.fields["bed"].queryset

    # Service layer must reject allocation to unassigned hostel
    from apps.hostels.services import assign_student_to_bed
    from django.core.exceptions import ValidationError
    student = User.objects.filter(groups__name="Student").first()
    if student:
        with pytest.raises(ValidationError, match="assigned"):
            assign_student_to_bed(
                student=student,
                bed=unassigned_bed,
                created_by=warden_user,
            )


@pytest.mark.django_db
def test_hostel_deactivation_prevented_when_occupied(hostel_setup, student_user):
    """Verify infrastructure edit prevents deactivating a hostel with active occupants."""
    from apps.hostels.services import update_hostel
    from django.core.exceptions import ValidationError
    # Create active occupancy
    StudentOccupancy.objects.create(
        student=student_user,
        bed=hostel_setup["bed_a"],
        institution=student_user.institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        source=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
    )
    # Attempting to deactivate North Wing must raise ValidationError
    with pytest.raises(ValidationError, match="Cannot deactivate hostel"):
        update_hostel(
            hostel=hostel_setup["hostel"],
            name="North Wing",
            resident_category=hostel_setup["hostel"].resident_category,
            address="Campus",
            description="",
            status=Hostel.Status.INACTIVE,
        )


@pytest.mark.django_db
def test_chief_warden_infrastructure_mutation_denied(client, chief_warden_user, hostel_setup):
    """Verify Chief Warden retains oversight but is denied infrastructure creation and modification."""
    from apps.hostels.services import create_hostel, update_hostel, create_room
    from django.core.exceptions import ValidationError

    client.force_login(chief_warden_user)

    # Views must return 403 Forbidden
    assert client.get(reverse("hostels:hostel_create")).status_code == 403
    assert client.post(reverse("hostels:hostel_create"), {"name": "Blocked", "code": "BLK-99"}).status_code == 403
    assert client.get(reverse("hostels:hostel_edit", kwargs={"pk": hostel_setup["hostel"].id})).status_code == 403
    assert client.get(reverse("hostels:room_create")).status_code == 403

    # Services must raise ValidationError when called by Chief Warden
    with pytest.raises(ValidationError, match="authorized to create hostels"):
        create_hostel(
            institution=chief_warden_user.institution,
            name="Blocked Hostel",
            code="BLK-01",
            acting_user=chief_warden_user,
        )

    with pytest.raises(ValidationError, match="permission to edit this hostel"):
        update_hostel(
            hostel=hostel_setup["hostel"],
            name="Edited Name",
            resident_category=hostel_setup["hostel"].resident_category,
            address="Campus",
            description="",
            status=Hostel.Status.ACTIVE,
            acting_user=chief_warden_user,
        )

    with pytest.raises(ValidationError, match="permission to create rooms"):
        create_room(
            floor=hostel_setup["floor"],
            room_number="999",
            room_type="single",
            acting_user=chief_warden_user,
        )

    # Chief Warden dashboard still renders successfully with oversight metrics
    resp = client.get(reverse("accounts:dashboard_chief_warden"))
    assert resp.status_code == 200
    assert "Chief Warden Authority Scope" in resp.content.decode()


@pytest.mark.django_db
def test_hostel_admin_routine_occupancy_denied(client, admin_user, student_user, hostel_setup):
    """Verify Hostel Administrator cannot perform routine bed allocations or releases."""
    from apps.hostels.services import assign_student_to_bed, release_student_occupancy
    from django.core.exceptions import ValidationError

    client.force_login(admin_user)

    # Generic assignment view must return 403
    assert client.get(reverse("hostels:occupancy_assign")).status_code == 403
    assert client.post(reverse("hostels:occupancy_assign"), {
        "student": student_user.id,
        "bed": hostel_setup["bed_a"].id,
    }).status_code == 403

    # Direct service call to assign_student_to_bed must raise ValidationError
    with pytest.raises(ValidationError, match="Hostel Administrators cannot perform routine bed allocations"):
        assign_student_to_bed(
            student=student_user,
            bed=hostel_setup["bed_a"],
            created_by=admin_user,
        )

    # Create an occupancy record directly
    occ = StudentOccupancy.objects.create(
        student=student_user,
        bed=hostel_setup["bed_a"],
        institution=admin_user.institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        source=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
    )

    # Generic release view must return 403
    assert client.get(reverse("hostels:occupancy_release", kwargs={"pk": occ.id})).status_code == 403

    # Direct service call to release_student_occupancy must raise ValidationError
    with pytest.raises(ValidationError, match="Hostel Administrators cannot perform routine occupancy releases"):
        release_student_occupancy(
            occupancy_id=occ.id,
            release_reason="Routine clearance requested",
            acting_user=admin_user,
        )


@pytest.mark.django_db
def test_dean_and_sysadmin_cannot_gain_allocation_via_superuser(client, dean_user, sysadmin_user, student_user, hostel_setup):
    """Verify superuser and is_staff flags DO NOT grant allocation authority to Dean or SysAdmin."""
    from apps.hostels.services import assign_student_to_bed
    from django.core.exceptions import ValidationError

    # Grant superuser and staff flags to Dean
    dean_user.is_superuser = True
    dean_user.is_staff = True
    dean_user.save()

    client.force_login(dean_user)
    assert client.get(reverse("hostels:occupancy_assign")).status_code == 403
    with pytest.raises(ValidationError, match="Dean of Student Welfare is not permitted"):
        assign_student_to_bed(
            student=student_user,
            bed=hostel_setup["bed_a"],
            created_by=dean_user,
        )

    # Test SysAdmin (already superuser and staff)
    client.force_login(sysadmin_user)
    assert client.get(reverse("hostels:occupancy_assign")).status_code == 403
    with pytest.raises(ValidationError, match="System Administrators are not permitted"):
        assign_student_to_bed(
            student=student_user,
            bed=hostel_setup["bed_a"],
            created_by=sysadmin_user,
        )


@pytest.mark.django_db
def test_sysadmin_audit_log_filters_operational_events(client, sysadmin_user, student_user, hostel_setup):
    """Verify System Administrator technical dashboard excludes operational student occupancy data."""
    from apps.core.models import AuditLog

    # Create technical log
    AuditLog.objects.create(
        acting_user=sysadmin_user,
        institution=sysadmin_user.institution,
        action="USER_LOGIN",
        target_type="User",
        target_id=str(sysadmin_user.id),
        change_summary="User sysadmin logged in successfully",
    )

    # Create operational student occupancy log containing sensitive student info
    AuditLog.objects.create(
        acting_user=sysadmin_user,
        institution=sysadmin_user.institution,
        action="OCCUPANCY_ASSIGNED",
        target_type="StudentOccupancy",
        target_id="9999",
        change_summary="Sensitive welfare assignment for student Jane Doe",
    )

    client.force_login(sysadmin_user)
    resp = client.get(reverse("accounts:dashboard_sysadmin"))
    assert resp.status_code == 200
    content = resp.content.decode()

    assert "USER_LOGIN" in content
    assert "User sysadmin logged in successfully" in content
    # Operational occupant record must NOT be exposed
    assert "Sensitive welfare assignment for student Jane Doe" not in content
    assert "OCCUPANCY_ASSIGNED" not in content


@pytest.mark.django_db
def test_capacity_metrics_distinguishes_maintenance_and_operational_beds(hostel_setup, student_user):
    """Verify total capacity, occupied beds, and available operational beds are accurately differentiated."""
    from apps.hostels.selectors import get_hostel_queryset_with_stats
    from apps.hostels.models import OperationalStatus

    # Bed A is occupied
    StudentOccupancy.objects.create(
        student=student_user,
        bed=hostel_setup["bed_a"],
        institution=student_user.institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        source=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
    )

    # Bed B is under maintenance
    hostel_setup["bed_b"].operational_status = OperationalStatus.MAINTENANCE
    hostel_setup["bed_b"].save()

    hostel_qs = get_hostel_queryset_with_stats(institution=student_user.institution)
    h = hostel_qs.get(id=hostel_setup["hostel"].id)

    assert h.total_beds == 2
    assert h.occupied_beds == 1
    assert h.maintenance_beds == 1
    # Available operational beds = total (2) - occupied (1) - maintenance (1) = 0
    available_operational = max(0, h.total_beds - h.occupied_beds - h.maintenance_beds)
    assert available_operational == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fixture_name,dashboard_url,expected_links,forbidden_links,expected_theme_bg",
    [
        (
            "student_user",
            "accounts:dashboard_student",
            [
                "Dashboard",
                "My Exchange Requests",
                "My Exchange Proposals",
                "Transfer Orders",
                "Notifications",
                "Profile",
            ],
            [
                "Hostels Explorer",
                "Room Inventory",
                "Beds & Status",
                "Occupancy Roster",
                "Exchange Approvals",
                "Eligibility Verification",
                "Exchange Policies",
                "System Console",
                "Student Directory",
            ],
            "bg-[#702C3B]",
        ),
        (
            "warden_user",
            "accounts:dashboard_warden",
            [
                "Dashboard",
                "Hostels Explorer",
                "Room Inventory",
                "Beds & Status",
                "Occupancy Roster",
                "Exchange Approvals",
                "Transfer Orders",
            ],
            [
                "System Console",
                "Student Directory",
                "Eligibility Verification",
                "Exchange Policies",
            ],
            "bg-[#702C3B]",
        ),
        (
            "chief_warden_user",
            "accounts:dashboard_chief_warden",
            [
                "Dashboard",
                "Hostels Explorer",
                "Room Inventory",
                "Beds & Status",
                "Occupancy Roster",
                "Exchange Approvals",
                "Transfer Orders",
            ],
            [
                "System Console",
                "Student Directory",
                "Eligibility Verification",
                "Exchange Policies",
            ],
            "bg-[#702C3B]",
        ),
        (
            "admin_user",
            "accounts:dashboard_admin",
            [
                "Dashboard",
                "Hostels Explorer",
                "Room Inventory",
                "Beds & Status",
                "Occupancy Roster",
                "Eligibility Verification",
                "Exchange Policies",
            ],
            [
                "Exchange Approvals",
                "System Console",
                "Student Directory",
            ],
            "bg-[#702C3B]",
        ),
        (
            "dean_user",
            "accounts:dashboard_dean",
            [
                "Dashboard",
                "Hostels Explorer",
                "Room Inventory",
                "Beds & Status",
                "Occupancy Roster",
                "Exchange Approvals",
                "Transfer Orders",
            ],
            [
                "System Console",
                "Student Directory",
                "Eligibility Verification",
                "Exchange Policies",
            ],
            "bg-[#702C3B]",
        ),
        (
            "sysadmin_user",
            "accounts:dashboard_sysadmin",
            [
                "System Console",
                "Django Admin",
                "Student Directory",
                "Add Student",
                "Import Students",
            ],
            [
                "Hostels Explorer",
                "Room Inventory",
                "Beds & Status",
                "Occupancy Roster",
                "Exchange Approvals",
                "Transfer Orders",
                "Eligibility Verification",
                "Exchange Policies",
                "Phase 2",
            ],
            "bg-[#702C3B]",
        ),
    ],
)
def test_all_dashboards_sidebar_integrity_and_role_scoping(
    client,
    request,
    fixture_name,
    dashboard_url,
    expected_links,
    forbidden_links,
    expected_theme_bg,
    hostel_setup,
):
    """Verify all 6 dashboards render a single, conflict-free, role-accurate sidebar with 1 active item."""
    user = request.getfixturevalue(fixture_name)
    client.force_login(user)
    resp = client.get(reverse(dashboard_url))
    assert resp.status_code == 200
    html = resp.content.decode()

    # 1. Zero git conflict markers
    assert "<<<<<<<" not in html
    assert "=======" not in html
    assert ">>>>>>>" not in html

    # 2. Exactly one sidebar element rendered
    assert html.count('id="app-sidebar"') == 1
    assert html.count("<aside") == 1

    # 3. Correct role-themed sidebar
    assert expected_theme_bg in html

    # Extract sidebar content
    sidebar_html = html.split('id="app-sidebar"')[1].split("</aside>")[0]

    # 4. Each expected link is present exactly once in sidebar
    for link_text in expected_links:
        count = sidebar_html.count(f">{link_text}<")
        assert count == 1, f"Expected '{link_text}' to appear exactly once in sidebar for {fixture_name}, found {count}"

    # 5. Forbidden links are strictly absent
    for link_text in forbidden_links:
        assert link_text not in sidebar_html, f"Forbidden '{link_text}' found in sidebar for {fixture_name}"

    # 6. Exactly single active-page highlight in sidebar
    assert sidebar_html.count('aria-current="page"') == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fixture_name,dashboard_url,role_eyebrow",
    [
        ("student_user", "accounts:dashboard_student", "Student Portal"),
        ("warden_user", "accounts:dashboard_warden", "Warden Portal"),
        ("chief_warden_user", "accounts:dashboard_chief_warden", "Chief Warden Portal"),
        ("admin_user", "accounts:dashboard_admin", "Hostel Admin Portal"),
        ("dean_user", "accounts:dashboard_dean", "Dean Portal"),
        ("sysadmin_user", "accounts:dashboard_sysadmin", "System Admin Portal"),
    ],
)
def test_all_dashboards_shared_design_system_and_warden_consistency(
    client,
    request,
    fixture_name,
    dashboard_url,
    role_eyebrow,
    hostel_setup,
):
    """Verify all 6 dashboards use the shared Warden design system and have zero overflow."""
    user = request.getfixturevalue(fixture_name)
    client.force_login(user)
    resp = client.get(reverse(dashboard_url))
    assert resp.status_code == 200
    html = resp.content.decode()

    # 1. Shared design system shell & tokens
    assert "warden-shell" in html
    assert "roomloop-dashboard-shell" in html
    assert role_eyebrow in html
    assert "#702C3B" in html
    assert "#E8DED4" in html

    # 2. Universal topbar notification bell & profile dropdown
    assert 'id="notification-bell-btn"' in html
    assert 'id="notification-dropdown"' in html
    assert 'id="user-menu-button"' in html

    # 3. Active navigation highlight
    assert "sidebar-nav-item" in html
    assert "active" in html
    assert 'aria-current="page"' in html


@pytest.mark.django_db
def test_dashboard_stats_api_unauthenticated_redirects(client):
    """Verify unauthenticated requests to dashboard_stats_api are redirected to login."""
    resp = client.get(reverse("accounts:dashboard_stats_api"))
    assert resp.status_code == 302
    assert reverse("accounts:login") in resp.url


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fixture_name,expected_keys",
    [
        ("student_user", ["available_hostels", "eligible_category", "active_requests", "incoming_proposals"]),
        ("warden_user", ["vacant_beds", "pending_reviews_count", "occupied_beds", "occupancy_pct", "total_beds", "total_rooms"]),
        ("chief_warden_user", ["total_hostels", "total_beds", "occupied_beds", "available_beds", "occupancy_pct"]),
        ("admin_user", ["total_hostels", "total_rooms", "total_beds", "occupied_beds", "available_beds", "pending_eligibility_count"]),
        ("dean_user", ["total_hostels", "total_rooms", "total_beds", "occupied_beds", "available_beds", "occupancy_pct"]),
        ("sysadmin_user", ["total_users", "active_users", "system_status"]),
    ],
)
def test_dashboard_stats_api_all_roles(client, request, fixture_name, expected_keys, hostel_setup):
    """Verify live statistics API endpoint returns HTTP 200 and schema for all 6 roles."""
    user = request.getfixturevalue(fixture_name)
    client.force_login(user)
    resp = client.get(reverse("accounts:dashboard_stats_api"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "stats" in data
    for key in expected_keys:
        assert key in data["stats"], f"Missing expected key '{key}' in stats for role {user.primary_role}"



