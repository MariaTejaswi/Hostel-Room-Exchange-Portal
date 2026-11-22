import pytest
from django.urls import reverse
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.db import connection

from apps.accounts.models import User
from apps.accounts.services import (
    create_user_with_role,
    ROLE_STUDENT,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_HOSTEL_ADMIN,
    ROLE_DEAN,
    ROLE_SYSADMIN,
)
from apps.hostels.models import Hostel, WardenAssignment
from apps.core.models import AuditLog


@pytest.fixture
def test_users(institution, roles):
    """Seed all 6 role users for performance and auth testing."""
    users = {}
    role_specs = [
        ("student_p5@apex.test", "Student P5", ROLE_STUDENT, "REG-P5-01"),
        ("warden_p5@apex.test", "Warden P5", ROLE_WARDEN, None),
        ("cw_p5@apex.test", "Chief Warden P5", ROLE_CHIEF_WARDEN, None),
        ("admin_p5@apex.test", "Hostel Admin P5", ROLE_HOSTEL_ADMIN, None),
        ("dean_p5@apex.test", "Dean P5", ROLE_DEAN, None),
        ("sysadmin_p5@apex.test", "SysAdmin P5", ROLE_SYSADMIN, None),
    ]
    for email, name, r_name, reg in role_specs:
        u = User.objects.filter(email=email).first()
        if not u:
            u = create_user_with_role(
                email=email,
                full_name=name,
                role_name=r_name,
                institution=institution,
                password="TestPassword123!",
                registration_number=reg,
                residential_eligibility=User.ResidentialEligibility.MALE if r_name == ROLE_STUDENT else User.ResidentialEligibility.UNVERIFIED,
                is_staff=r_name in [ROLE_HOSTEL_ADMIN, ROLE_SYSADMIN],
            )
            if r_name == ROLE_SYSADMIN:
                u.is_superuser = True
                u.save()
        users[r_name] = u
    return users


@pytest.mark.django_db
def test_successful_login_and_redirect_all_six_roles(client, test_users):
    """Verify that all six roles successfully authenticate and redirect to their exact dashboards."""
    expected_dashboards = {
        ROLE_STUDENT: "/dashboard/student/",
        ROLE_WARDEN: "/dashboard/warden/",
        ROLE_CHIEF_WARDEN: "/dashboard/chief-warden/",
        ROLE_HOSTEL_ADMIN: "/dashboard/admin/",
        ROLE_DEAN: "/dashboard/dean/",
        ROLE_SYSADMIN: "/dashboard/sysadmin/",
    }

    for role_name, user in test_users.items():
        resp = client.post(
            reverse("accounts:login"),
            {"email": user.email, "password": "TestPassword123!"},
            follow=True,
        )
        assert resp.status_code == 200
        assert resp.wsgi_request.user.is_authenticated
        assert resp.wsgi_request.user.email == user.email
        assert expected_dashboards[role_name] in resp.redirect_chain[0][0]
        client.logout()


@pytest.mark.django_db
def test_all_dashboards_hide_stale_phase_and_soon_labels(test_users):
    """Completed functionality must not be presented as a future phase."""
    dashboard_urls = {
        ROLE_STUDENT: reverse("accounts:dashboard_student"),
        ROLE_WARDEN: reverse("accounts:dashboard_warden"),
        ROLE_CHIEF_WARDEN: reverse("accounts:dashboard_chief_warden"),
        ROLE_HOSTEL_ADMIN: reverse("accounts:dashboard_admin"),
        ROLE_DEAN: reverse("accounts:dashboard_dean"),
        ROLE_SYSADMIN: reverse("accounts:dashboard_sysadmin"),
    }

    for role_name, url in dashboard_urls.items():
        role_client = Client()
        role_client.force_login(test_users[role_name])
        response = role_client.get(url)

        assert response.status_code == 200
        rendered_html = response.content.decode()
        assert "Phase 1" not in rendered_html
        assert "Phase 2" not in rendered_html
        assert ">Soon<" not in rendered_html.replace(" ", "")


@pytest.mark.django_db
def test_unauthorized_dashboard_access_denied(client, test_users):
    """Verify that unauthorized roles are forbidden from accessing restricted dashboards."""
    student = test_users[ROLE_STUDENT]
    client.force_login(student)

    restricted_urls = [
        reverse("accounts:dashboard_warden"),
        reverse("accounts:dashboard_chief_warden"),
        reverse("accounts:dashboard_admin"),
        reverse("accounts:dashboard_dean"),
        reverse("accounts:dashboard_sysadmin"),
        reverse("accounts:student_list"),
        reverse("accounts:eligibility_verification_list"),
    ]

    for url in restricted_urls:
        resp = client.get(url)
        # Expect HTTP 403 Forbidden
        assert resp.status_code == 403, f"Expected 403 for Student on {url}, got {resp.status_code}"


@pytest.mark.django_db
def test_user_role_instance_caching_isolation(institution, roles):
    """Verify that primary_role caching remains isolated per user instance and cannot leak across roles."""
    u1 = create_user_with_role(
        email="iso1@apex.test", full_name="User 1", role_name=ROLE_STUDENT, institution=institution, password="Password123!", registration_number="REG-ISO-01"
    )
    u2 = create_user_with_role(
        email="iso2@apex.test", full_name="User 2", role_name=ROLE_WARDEN, institution=institution, password="Password123!"
    )

    assert u1.primary_role == "Student"
    assert u2.primary_role == "Warden"
    assert u1.is_student_role is True
    assert u1.is_warden_role is False
    assert u2.is_warden_role is True
    assert u2.is_student_role is False
    assert u1._cached_primary_role == "Student"
    assert u2._cached_primary_role == "Warden"


@pytest.mark.django_db
def test_dashboard_query_count_ceilings(client, test_users, institution):
    """Assert that query counts on all 6 dashboards remain strictly below optimized ceilings."""
    # Assign warden to a hostel
    hostel, _ = Hostel.objects.get_or_create(
        institution=institution, code="W-CEIL-01", defaults={"name": "Ceiling Hostel", "resident_category": Hostel.ResidentCategory.MALE}
    )
    WardenAssignment.objects.get_or_create(warden=test_users[ROLE_WARDEN], hostel=hostel, defaults={"is_primary": True})

    ceilings = {
        ROLE_STUDENT: (reverse("accounts:dashboard_student"), 12),
        ROLE_WARDEN: (reverse("accounts:dashboard_warden"), 30),
        ROLE_CHIEF_WARDEN: (reverse("accounts:dashboard_chief_warden"), 12),
        ROLE_HOSTEL_ADMIN: (reverse("accounts:dashboard_admin"), 12),
        ROLE_SYSADMIN: (reverse("accounts:dashboard_sysadmin"), 10),
        ROLE_DEAN: (reverse("accounts:dashboard_dean"), 10),
    }

    for role_name, (url, max_queries) in ceilings.items():
        user = test_users[role_name]
        c = Client()
        c.force_login(user)

        with CaptureQueriesContext(connection) as ctx:
            resp = c.get(url)

        assert resp.status_code == 200
        query_count = len(ctx.captured_queries)
        assert query_count <= max_queries, (
            f"Query ceiling exceeded for {role_name} on {url}: expected <= {max_queries}, got {query_count}"
        )
