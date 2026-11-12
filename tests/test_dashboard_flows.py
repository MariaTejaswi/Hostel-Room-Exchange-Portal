import pytest
from django.test import Client
from django.urls import reverse
from apps.accounts.services import create_user_with_role, ROLE_WARDEN
from apps.hostels.models import StudentOccupancy


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
    """Verify system administrator accesses admin dashboard."""
    csrf_client = Client(enforce_csrf_checks=True)
    login_get = csrf_client.get(reverse("accounts:login"))
    token = login_get.cookies["csrftoken"].value

    response = csrf_client.post(
        reverse("accounts:login"),
        {"email": sysadmin_user.email, "password": "TestPassword123!", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:dashboard_admin")

    dash_resp = csrf_client.get(response.url)
    assert dash_resp.status_code == 200


@pytest.mark.django_db
def test_dean_dashboard_flow(client, dean_user, hostel_setup):
    """Verify dean accesses executive read-only analytics."""
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
    assert "Dean of Student Welfare Analytics" in dash_resp.content.decode()


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
    # Student cannot access admin, warden, or chief-warden dashboard
    client.force_login(student_user)
    assert client.get(reverse("accounts:dashboard_admin")).status_code == 403
    assert client.get(reverse("accounts:dashboard_warden")).status_code == 403
    assert client.get(reverse("accounts:dashboard_chief_warden")).status_code == 403

    # Warden cannot access admin dashboard
    client.force_login(warden_user)
    assert client.get(reverse("accounts:dashboard_admin")).status_code == 403
