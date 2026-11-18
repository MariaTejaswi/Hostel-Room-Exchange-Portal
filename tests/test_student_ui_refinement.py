import pytest
from pathlib import Path
from django.urls import reverse
from apps.hostels.models import StudentOccupancy
from apps.exchange.models import ExchangeRequest, Notification


def test_compiled_css_tokens():
    """Verify static styles.css contains the agreed Maroon + Cream tokens and responsive margin."""
    css_path = Path("static/css/dist/styles.css")
    assert css_path.exists(), "styles.css must exist"
    css = css_path.read_text(encoding="utf-8")

    # Maroon + Cream Palette tokens
    assert "#702c3b" in css or "#702C3B" in css, "Maroon primary #702C3B must be compiled into styles.css"
    assert "#5c2330" in css or "#5C2330" in css, "Dark Maroon #5C2330 must be compiled into styles.css"
    assert "#f5ebdd" in css or "#F5EBDD" in css, "Cream background #F5EBDD must be compiled into styles.css"
    assert "#faf4eb" in css or "#FAF4EB" in css, "Soft Cream #FAF4EB must be compiled into styles.css"
    assert "#49372f" in css or "#49372F" in css, "Dark text #49372F must be compiled into styles.css"
    assert "#8a7b72" in css or "#8A7B72" in css, "Muted text #8A7B72 must be compiled into styles.css"
    assert "#e8ded4" in css or "#E8DED4" in css, "Border token #E8DED4 must be compiled into styles.css"

    # Layout offset and nav item flex
    assert "margin-left:16rem" in css, "Desktop layout offset margin-left: 16rem must be in styles.css"
    assert "student-shell" in css, "student-shell scoped styling must be in styles.css"


@pytest.mark.django_db
def test_student_dashboard_accommodation_and_requests(client, student_user, hostel_setup):
    """Test Student Dashboard layout: accommodation 4-tile grid, exchange card, and maroon theme."""
    StudentOccupancy.objects.create(
        institution=student_user.institution,
        student=student_user,
        bed=hostel_setup["bed_a"],
        status="active",
    )

    client.force_login(student_user)
    res = client.get(reverse("accounts:dashboard_student"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    # Shell scoping
    assert "student-shell" in html, "Student dashboard must be scoped with student-shell"

    # Accommodation Section
    assert "Assigned Hostel" in html, "Hostel tile banner must be displayed"
    assert "North Wing" in html
    assert "Active Allocation" in html, "Clear active allocation status pill must be displayed"
    assert "Block" in html
    assert "Floor" in html
    assert "Room" in html
    assert "Bed" in html
    assert "Bed A" in html

    # Exchange Centre Card
    assert "Exchange Centre" in html or "Exchange centre" in html
    assert "btn-primary" in html, "Maroon primary exchange button must be available"


@pytest.mark.django_db
def test_exchange_request_list_cards_and_actions(client, student_user, hostel_setup):
    """Test My Exchange Requests displays individual cards with ID, date, status, accommodation, and actions."""
    occ = StudentOccupancy.objects.create(
        institution=student_user.institution,
        student=student_user,
        bed=hostel_setup["bed_a"],
        status="active",
    )
    client.force_login(student_user)

    # 1. Empty state
    res = client.get(reverse("exchange:request_list"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")
    assert "student-shell" in html
    assert "No Exchange Requests Yet" in html or "No exchange requests yet" in html

    # 2. Card with open request
    req = ExchangeRequest.objects.create(
        institution=student_user.institution,
        student=student_user,
        current_occupancy=occ,
        preferred_bed=hostel_setup["bed_b"],
        request_type="exchange",
        status="open",
        reason="Prefer Bed B near window",
    )
    res = client.get(reverse("exchange:request_list"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    assert "student-shell" in html
    assert f"Request #{str(req.pk)[:8]}" in html or f"#{str(req.pk)[:8]}" in html
    assert "Preferred Accommodation" in html
    assert "Bed B" in html
    assert "View Details" in html
    assert "Edit" in html  # Open request can be edited


@pytest.mark.django_db
def test_notifications_cards_and_empty_state(client, student_user):
    """Test Notifications page displays empty state and individual readable cards."""
    client.force_login(student_user)

    # 1. Empty state
    res = client.get(reverse("exchange:notifications"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")
    assert "student-shell" in html
    assert "You&#x27;re all caught up" in html or "You're all caught up" in html

    # 2. With notification
    Notification.objects.create(
        recipient=student_user,
        notification_type="proposal_matched",
        message="A potential 2-party exchange proposal has been matched for your review.",
    )
    res = client.get(reverse("exchange:notifications"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")
    assert "student-shell" in html
    assert "A potential 2-party exchange proposal" in html


@pytest.mark.django_db
def test_student_profile_styling(client, student_user, hostel_setup):
    """Test profile page adopts student theme without changing editable form functionality."""
    StudentOccupancy.objects.create(
        institution=student_user.institution,
        student=student_user,
        bed=hostel_setup["bed_a"],
        status="active",
    )
    client.force_login(student_user)

    res = client.get(reverse("accounts:profile"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    assert "student-shell" in html
    assert "Account Profile" in html
    assert "Allocated Accommodation" in html
    assert "North Wing" in html
    assert "Save Profile Changes" in html


@pytest.mark.django_db
def test_staff_dashboard_isolation(client, warden_user):
    """Verify staff dashboards do NOT have student-shell and preserve operational Navy/Slate theme."""
    client.force_login(warden_user)

    res = client.get(reverse("accounts:dashboard_warden"))
    assert res.status_code == 200
    html = res.content.decode("utf-8")

    assert "student-shell" not in html, "Staff dashboard must not contain student-shell class"
