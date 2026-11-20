import pytest
from django.urls import reverse
from django.utils import timezone
from apps.exchange.models import Notification


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fixture_name,dashboard_url",
    [
        ("student_user", "accounts:dashboard_student"),
        ("warden_user", "accounts:dashboard_warden"),
        ("chief_warden_user", "accounts:dashboard_chief_warden"),
        ("admin_user", "accounts:dashboard_admin"),
        ("dean_user", "accounts:dashboard_dean"),
        ("sysadmin_user", "accounts:dashboard_sysadmin"),
    ],
)
def test_notification_bell_rendered_near_profile_for_all_roles(
    client, request, fixture_name, dashboard_url, hostel_setup
):
    """Verify notification bell and dropdown are present in topbar near profile for all 6 roles."""
    user = request.getfixturevalue(fixture_name)
    client.force_login(user)

    response = client.get(reverse(dashboard_url))
    assert response.status_code == 200
    html = response.content.decode()

    # Notification bell container & button near profile
    assert 'id="notification-bell-container"' in html
    assert 'id="notification-bell-btn"' in html
    assert 'id="notification-dropdown"' in html
    assert reverse("exchange:notifications") in html

    # Also verify the full notifications page is accessible for this user role
    notif_resp = client.get(reverse("exchange:notifications"))
    assert notif_resp.status_code == 200
    assert "Notifications" in notif_resp.content.decode()


@pytest.mark.django_db
def test_unread_notification_badge_and_mark_all_read(client, warden_user):
    """Verify unread badge increments and mark-all-as-read clears unread state."""
    # Create 2 unread notifications for the warden
    n1 = Notification.objects.create(
        recipient=warden_user,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
        message="New same-hostel exchange proposal submitted.",
    )
    n2 = Notification.objects.create(
        recipient=warden_user,
        notification_type=Notification.NotificationType.APPROVAL_UPDATED,
        message="A student accepted proposal in your block.",
    )

    client.force_login(warden_user)
    response = client.get(reverse("accounts:dashboard_warden"))
    assert response.status_code == 200
    html = response.content.decode()

    # Bell shows unread count badge
    assert "2" in html
    assert "New same-hostel exchange proposal submitted." in html

    # Mark all as read via POST
    post_resp = client.post(reverse("exchange:notifications_mark_read"))
    assert post_resp.status_code in (302, 200)

    # Refresh dashboard
    refreshed = client.get(reverse("accounts:dashboard_warden"))
    assert refreshed.status_code == 200
    r_html = refreshed.content.decode()

    # Unread notifications count in db is now 0
    assert Notification.objects.filter(recipient=warden_user, read_at__isnull=True).count() == 0
    # No active unread badge
    assert 'class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-[#702C3B] text-white"' not in r_html


@pytest.mark.django_db
def test_mark_single_notification_read_ajax(client, student_user):
    """Verify marking single notification as read via AJAX returns JSON status."""
    n = Notification.objects.create(
        recipient=student_user,
        notification_type=Notification.NotificationType.ACCEPTANCE_REQUIRED,
        message="Action required on room exchange proposal.",
    )

    client.force_login(student_user)
    resp = client.post(
        reverse("exchange:notifications_mark_read"),
        {"notification_id": str(n.id)},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["unread_count"] == 0

    n.refresh_from_db()
    assert n.read_at is not None
