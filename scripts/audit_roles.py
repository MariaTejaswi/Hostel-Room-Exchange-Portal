import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from django.test import Client
from django.urls import reverse
from apps.accounts.models import User
from apps.accounts.services import create_user_with_role, ROLE_WARDEN
from apps.core.models import Institution

def run_audits():
    client = Client(enforce_csrf_checks=True)
    password = "RoomLoop2026!"

    print("=== AUDIT 1: FAVICON TEST ===")
    fav_resp = client.get("/favicon.ico")
    print(f"GET /favicon.ico status: {fav_resp.status_code}")
    if fav_resp.status_code in (301, 302):
        print(f"Redirect target: {fav_resp.url}")
        assert "logo.svg" in fav_resp.url
    elif fav_resp.status_code == 200:
        print("Favicon returned 200 OK directly.")
    else:
        raise AssertionError(f"Unexpected favicon status: {fav_resp.status_code}")

    print("\n=== AUDIT 2: ROLE LOGIN AND DASHBOARD FLOWS ===")
    test_roles = [
        ("System Administrator", "admin@roomloop.edu", "/dashboard/sysadmin/"),
        ("Hostel Administrator", "hostel.admin@roomloop.edu", "/dashboard/admin/"),
        ("Chief Warden", "chief.warden@roomloop.edu", "/dashboard/chief-warden/"),
        ("Warden (Assigned)", "warden.north@roomloop.edu", "/dashboard/warden/"),
        ("Dean", "dean@roomloop.edu", "/dashboard/dean/"),
        ("Student (Allocated)", "arun.kumar@student.roomloop.edu", "/dashboard/student/"),
        ("Student (Unallocated)", "rohit.sharma@student.roomloop.edu", "/dashboard/student/"),
    ]

    for role_label, email, expected_dashboard in test_roles:
        # Start fresh session
        client = Client(enforce_csrf_checks=True)
        login_get = client.get(reverse("accounts:login"))
        assert login_get.status_code == 200, f"Login GET failed for {role_label}: {login_get.status_code}"
        
        csrf_token = login_get.cookies.get("csrftoken")
        assert csrf_token is not None, f"No CSRF cookie set for {role_label}"

        # Submit POST with CSRF
        login_post = client.post(
            reverse("accounts:login"),
            {
                "email": email,
                "password": password,
                "csrfmiddlewaretoken": csrf_token.value,
            },
            follow=False,
        )
        assert login_post.status_code == 302, f"Login POST failed for {role_label}: {login_post.status_code}"
        assert login_post.url == expected_dashboard, f"Expected redirect {expected_dashboard}, got {login_post.url}"

        # Follow to dashboard
        dash_resp = client.get(login_post.url)
        assert dash_resp.status_code == 200, f"Dashboard HTTP {dash_resp.status_code} for {role_label}"
        
        # Verify content
        content = dash_resp.content.decode("utf-8")
        assert "Traceback" not in content
        assert "500 Internal Server Error" not in content
        print(f"[{role_label}] {email} -> {login_post.url} -> HTTP 200 OK (Rendered {len(content)} bytes)")

        # Verify logout
        logout_resp = client.get(reverse("accounts:logout"))
        assert logout_resp.status_code == 302
        assert reverse("accounts:login") in logout_resp.url

        # Verify subsequent login
        login_get_again = client.get(reverse("accounts:login"))
        token2 = login_get_again.cookies.get("csrftoken").value
        login_again = client.post(
            reverse("accounts:login"),
            {"email": email, "password": password, "csrfmiddlewaretoken": token2},
        )
        assert login_again.status_code == 302
        assert login_again.url == expected_dashboard

    print("\n=== AUDIT 3: WARDEN WITHOUT ASSIGNED HOSTELS (EMPTY STATE) ===")
    inst = Institution.objects.first()
    unassigned_warden, created = User.objects.get_or_create(
        email="unassigned.warden@roomloop.edu",
        defaults={
            "full_name": "Unassigned Warden",
            "institution": inst,
        }
    )
    unassigned_warden.set_password(password)
    unassigned_warden.save()
    from django.contrib.auth.models import Group
    w_group, _ = Group.objects.get_or_create(name=ROLE_WARDEN)
    unassigned_warden.groups.add(w_group)

    client = Client(enforce_csrf_checks=True)
    l_get = client.get(reverse("accounts:login"))
    tok = l_get.cookies.get("csrftoken").value
    l_post = client.post(
        reverse("accounts:login"),
        {"email": unassigned_warden.email, "password": password, "csrfmiddlewaretoken": tok},
    )
    assert l_post.status_code == 302
    assert l_post.url == "/dashboard/warden/"
    warden_empty_resp = client.get(l_post.url)
    assert warden_empty_resp.status_code == 200
    w_content = warden_empty_resp.content.decode("utf-8")
    assert "You have not yet been assigned to supervise any hostels." in w_content
    print("Warden without assigned hostels empty state rendered successfully.")

    # Clean up unassigned_warden so DB remains as originally seeded
    unassigned_warden.delete()

    print("\n=== AUDIT 4: AUTHORIZATION ENFORCEMENT & INSTITUTION ISOLATION ===")
    # Student cannot access admin or warden dashboard
    client = Client(enforce_csrf_checks=True)
    student = User.objects.get(email="arun.kumar@student.roomloop.edu")
    client.force_login(student)
    assert client.get("/dashboard/admin/").status_code == 403
    assert client.get("/dashboard/warden/").status_code == 403
    assert client.get("/dashboard/chief-warden/").status_code == 403
    print("Student RBAC restrictions verified (HTTP 403 on staff dashboards).")

    # Warden cannot access admin dashboard
    warden = User.objects.get(email="warden.north@roomloop.edu")
    client.force_login(warden)
    assert client.get("/dashboard/admin/").status_code == 403
    print("Warden RBAC restrictions verified (HTTP 403 on admin dashboard).")

    # Unauthenticated user redirected
    client = Client(enforce_csrf_checks=True)
    for p in ["/dashboard/", "/dashboard/student/", "/dashboard/admin/", "/dashboard/warden/"]:
        resp = client.get(p)
        assert resp.status_code == 302
        assert "/login/" in resp.url
    print("Unauthenticated redirects verified.")

    print("\nALL AUDIT CHECKS PASSED PERFECTLY!")

if __name__ == "__main__":
    run_audits()
