import pytest
from django.urls import reverse
from django.test import override_settings
from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_STUDENT,
    ROLE_WARDEN,
    ROLE_CHIEF_WARDEN,
    ROLE_HOSTEL_ADMIN,
    ROLE_DEAN,
    ROLE_SYSADMIN,
    create_user_with_role,
    ensure_roles_exist,
)


@pytest.mark.django_db
def test_valid_login_succeeds(client, student_user):
    url = reverse("accounts:login")
    response = client.post(
        url,
        {"email": student_user.email, "password": "TestPassword123!"},
        follow=True,
    )
    assert response.status_code == 200
    assert response.wsgi_request.user.is_authenticated
    assert response.wsgi_request.user.email == student_user.email


@pytest.mark.django_db
def test_invalid_credentials_fail(client, student_user):
    url = reverse("accounts:login")
    response = client.post(
        url,
        {"email": student_user.email, "password": "WrongPassword!"},
    )
    assert response.status_code == 200
    assert not response.wsgi_request.user.is_authenticated
    assert "Invalid email or password" in response.content.decode()


@pytest.mark.django_db
def test_unauthenticated_user_redirected_to_login(client):
    protected_urls = [
        reverse("accounts:dashboard"),
        reverse("accounts:profile"),
        reverse("hostels:hostel_list"),
        reverse("hostels:room_inventory"),
    ]
    for url in protected_urls:
        response = client.get(url)
        assert response.status_code == 302
        assert reverse("accounts:login") in response.url


@pytest.mark.django_db
def test_logout_invalidates_session(client, student_user):
    client.force_login(student_user)
    response = client.get(reverse("accounts:logout"), follow=True)
    assert response.status_code == 200
    assert not response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
def test_password_reset_request_accepts_email(client, student_user):
    url = reverse("accounts:password_reset")
    response = client.post(url, {"email": student_user.email})
    assert response.status_code == 302
    assert response.url == "/password-reset/done/"


# =========================================================================
# Redesigned Login Page & Demo Accounts Tests
# =========================================================================

@pytest.mark.django_db
def test_login_page_renders_redesigned_theme_and_campus_image(client):
    """
    Verify login page visual style, campus image, HTML overlays, and form controls.
    """
    url = reverse("accounts:login")
    response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    # Coffee-brown color palette tokens
    assert "#F5EBDD" in content
    assert "#702C3B" in content
    assert "#35251F" in content
    assert "#E6D9CC" in content

    # Campus architectural image references (WebP + JPG fallback + responsive mobile/tablet variants)
    assert "roomloop-campus.webp" in content
    assert "roomloop-campus.jpg" in content
    assert "roomloop-campus-mobile.webp" in content
    assert "roomloop-campus-tablet.webp" in content
    assert 'fetchpriority="high"' in content
    assert 'width="896"' in content
    assert 'height="1200"' in content
    assert 'rel="preload"' in content

    # HTML overlay text and badges
    assert "Your campus. Your space." in content
    assert "Manage your accommodation and room exchanges in one place." in content
    assert "Verified student access" in content
    assert "Secure room exchanges" in content

    # Form heading & accessible controls
    assert "Welcome back" in content
    assert "University email" in content
    assert "password-toggle-btn" in content
    assert "type=\"button\"" in content  # Toggle must not submit form
    assert "Forgot password?" in content
    assert "Remember this workstation" in content
    assert "Sign in" in content

    # Layout tightening, typography, and contrast rules
    assert "max-w-[1160px]" in content
    assert "rounded-[20px]" in content
    assert "photo-overlay-gradient" in content
    assert "font-family: -apple-system, BlinkMacSystemFont" in content

    # Password visibility control positioned wrapper & accessible toggle
    assert "password-field-wrapper" in content
    assert "right: 12px; top: 50%; transform: translateY(-50%)" in content
    assert 'aria-label="Show password"' in content
    assert 'type="password"' in content
    assert "field-group" in content

    # Hidden scrollbar styling with functional scrolling
    assert "auth-scroll-hidden" in content
    assert "scrollbar-width: none" in content
    assert "auth-modal-scroll" in content

    # Reference visual structure elements
    assert "WELCOME TO ROOMLOOP" in content
    assert "Need help?" in content
    assert "New student? Contact your hostel administrator." in content
    assert "Secure institutional access" in content
    assert "Privacy Policy" in content
    assert "Help Centre" in content


@pytest.mark.django_db
def test_demo_accounts_panel_rendered_with_six_roles_when_enabled(client, institution):
    """
    Verify that with SHOW_DEMO_ACCOUNTS=True, all 6 demo shortcuts are displayed.
    """
    ensure_roles_exist()

    # Seed the 6 demo users
    roles = [
        ("arun.kumar@student.roomloop.edu", ROLE_STUDENT),
        ("warden.north@roomloop.edu", ROLE_WARDEN),
        ("chief.warden@roomloop.edu", ROLE_CHIEF_WARDEN),
        ("hostel.admin@roomloop.edu", ROLE_HOSTEL_ADMIN),
        ("dean@roomloop.edu", ROLE_DEAN),
        ("admin@roomloop.edu", ROLE_SYSADMIN),
    ]
    for email, role_name in roles:
        create_user_with_role(
            email=email,
            full_name=f"Demo {role_name}",
            role_name=role_name,
            institution=institution,
            password="RoomLoop2026!",
            registration_number="2024-NR-001" if role_name == ROLE_STUDENT else None,
        )

    with override_settings(SHOW_DEMO_ACCOUNTS=True):
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        assert "Try a demo account" in content
        assert "Choose a role to fill in the demo credentials." in content
        assert "Student" in content
        assert "Warden" in content
        assert "Chief Warden" in content
        assert "Hostel Administrator" in content
        assert "Dean" in content
        assert "System Administrator" in content

        # Accessible modal trigger, dialog container, and close button
        assert "demo-modal-open-btn" in content
        assert "demo-accounts-modal" in content
        assert 'role="dialog"' in content
        assert 'aria-modal="true"' in content
        assert "demo-modal-close-btn" in content
        assert "demo-selected-banner" in content
        assert "demo-selected-change-btn" in content

        # Repeated "Ready" badges and "Autofill shortcuts" pill must be completely removed
        assert "Autofill shortcuts" not in content
        assert "status-indicator" not in content
        assert ">Ready<" not in content
        assert "Ready" not in content or "Ready to sign in" in content  # Only the single active selection pill may mention ready

        # Check that emails are present in data attributes for autofill
        for email, _ in roles:
            assert email in content


@pytest.mark.django_db
def test_demo_accounts_setting_disabled_removes_panel_and_credentials(client, institution):
    """
    Requirement F: When SHOW_DEMO_ACCOUNTS=False:
    - Do not render demo panel.
    - Do not include credential mappings in page scripts, hidden HTML, or data attributes.
    - Standard login remains fully functional.
    """
    with override_settings(SHOW_DEMO_ACCOUNTS=False):
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200
        content = response.content.decode("utf-8")

        # Demo panel is completely absent
        assert "Try a demo account" not in content
        assert "Choose a role to fill in the demo credentials." not in content
        assert "demo-accounts-panel" not in content
        assert "demo-role-btn" not in content

        # No credential mappings leaked in HTML or scripts
        assert "arun.kumar@student.roomloop.edu" not in content
        assert "warden.north@roomloop.edu" not in content
        assert "chief.warden@roomloop.edu" not in content
        assert "RoomLoop2026!" not in content

        # Normal login form remains fully present
        assert "University email" in content
        assert "Sign in" in content


@pytest.mark.django_db
def test_all_six_roles_login_redirect_correctly(client, institution):
    """
    Verify each of the 6 scoped roles successfully logs in and redirects to its specific dashboard.
    """
    ensure_roles_exist()
    role_redirect_targets = [
        ("student.test@apex.test", ROLE_STUDENT, "/dashboard/student/"),
        ("warden.test@apex.test", ROLE_WARDEN, "/dashboard/warden/"),
        ("cw.test@apex.test", ROLE_CHIEF_WARDEN, "/dashboard/chief-warden/"),
        ("hadmin.test@apex.test", ROLE_HOSTEL_ADMIN, "/dashboard/admin/"),
        ("dean.test@apex.test", ROLE_DEAN, "/dashboard/dean/"),
        ("sysadmin.test@apex.test", ROLE_SYSADMIN, "/dashboard/sysadmin/"),
    ]

    for email, role_name, expected_dashboard in role_redirect_targets:
        user = create_user_with_role(
            email=email,
            full_name=f"User {role_name}",
            role_name=role_name,
            institution=institution,
            password="RoomLoop2026!",
            registration_number="REG-TEST-STU" if role_name == ROLE_STUDENT else None,
        )

        login_resp = client.post(
            reverse("accounts:login"),
            {"email": email, "password": "RoomLoop2026!"},
            follow=True,
        )
        assert login_resp.status_code == 200
        assert login_resp.wsgi_request.user.is_authenticated
        assert login_resp.wsgi_request.user.email == email
        # Check redirection URL or path
        assert expected_dashboard in login_resp.redirect_chain[0][0]
        client.logout()


@pytest.mark.django_db
def test_login_demo_accounts_query_caching(client):
    """
    Verify that demo accounts query caching reduces database load on consecutive requests.
    """
    from django.core.cache import cache
    cache.clear()

    url = reverse("accounts:login")
    # First request populates cache
    resp1 = client.get(url)
    assert resp1.status_code == 200
    cached = cache.get("roomloop_login_demo_active_emails")
    assert cached is not None

    # Second request hits cache successfully
    resp2 = client.get(url)
    assert resp2.status_code == 200
    assert cache.get("roomloop_login_demo_active_emails") == cached

