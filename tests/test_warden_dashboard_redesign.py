import pytest
from django.urls import reverse
from apps.hostels.models import StudentOccupancy, WardenAssignment, Hostel
from apps.exchange.models import ExchangeProposal, ProposalParticipant, ExchangeRequest


@pytest.mark.django_db
def test_warden_dashboard_coffee_brown_theme_and_static_hostel_badge(client, warden_user, hostel_setup):
    """
    Verify warden dashboard renders coffee-brown theme and a STATIC hostel badge with building icon.
    There must be NO dropdown, selector, or click interaction near the assigned hostel.
    """
    client.force_login(warden_user)
    response = client.get(reverse("accounts:dashboard_warden"))
    assert response.status_code == 200
    html = response.content.decode()
    assert 'href="#main-content"' in html
    assert 'id="main-content" tabindex="-1"' in html
    assert 'aria-label="Open user menu"' in html
    assert 'aria-label="Notifications (' in html

    # 1. Coffee-brown palette tokens
    assert "#702C3B" in html
    assert "#F5EBDD" in html
    assert "#35251F" in html
    assert "#E8DED4" in html

    # 2. Scoped warden-shell and sidebar structure
    assert "warden-shell" in html
    assert "Hostel overview" in html
    assert "Manage residents and same-hostel exchange requests" in html

    # 3. Static scope badge near assigned hostel (North Wing)
    assert "North Wing" in html
    # Building icon SVG path
    assert "M3 21h18M3 10h18M5 10v11M9 10v11M15 10v11M19 10v11M12 3L2 8h20L12 3z" in html
    # NO dropdown / select / arrow interaction
    assert "<select" not in html
    assert "dropdown-toggle" not in html
    assert "role=\"combobox\"" not in html
    assert "data-dropdown-toggle" not in html

    # 4. Summary cards present with live data
    assert "Total beds" in html
    assert "Occupied beds" in html
    assert "Vacant beds" in html
    assert "Pending reviews" in html

    # 5. Middle section: Requests awaiting review & Hostel snapshot
    assert "Requests awaiting review" in html
    assert "Hostel snapshot" in html

    # 6. Recent resident allocations card with search and roster link
    assert "Recent resident allocations" in html
    assert "View roster" in html
    assert reverse("hostels:occupancy_list") in html


@pytest.mark.django_db
def test_warden_dashboard_reviews_and_allocations_data_scoping(client, warden_user, student_user, hostel_setup):
    """
    Verify live proposal awaiting review and recent resident allocation appear on dashboard.
    """
    # Allocate student
    occ = StudentOccupancy.objects.create(
        student=student_user,
        bed=hostel_setup["bed_a"],
        institution=warden_user.institution,
        status=StudentOccupancy.OccupancyStatus.ACTIVE,
        source=StudentOccupancy.AllocationSource.ADMINISTRATIVE,
    )

    # Create a same-hostel pending proposal
    proposal = ExchangeProposal.objects.create(
        institution=warden_user.institution,
        status=ExchangeProposal.Status.PENDING_APPROVAL,
        cycle_length=2,
    )
    ProposalParticipant.objects.create(
        proposal=proposal,
        student=student_user,
        current_occupancy=occ,
        proposed_bed=hostel_setup["bed_b"],
        sequence_order=1,
        response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
    )

    client.force_login(warden_user)
    response = client.get(reverse("accounts:dashboard_warden"))
    assert response.status_code == 200
    html = response.content.decode()

    # Proposal appears in review list
    assert student_user.full_name in html
    assert "Direct Swap (2-Way)" in html
    assert reverse("exchange:approval_detail", kwargs={"pk": proposal.id}) in html

    # Occupancy appears in recent allocations
    assert f"Room {hostel_setup['bed_a'].room.room_number}" in html
    assert hostel_setup["bed_a"].bed_label in html
    assert reverse("hostels:occupancy_detail", kwargs={"pk": occ.id}) in html


@pytest.mark.django_db
def test_warden_unassigned_empty_state_rendering(client, institution, roles):
    """
    Verify warden without any assigned hostel sees polished empty state.
    """
    from apps.accounts.services import create_user_with_role, ROLE_WARDEN
    unassigned = create_user_with_role(
        email="unassigned.w2@apex.test",
        full_name="Unassigned Warden Two",
        role_name=ROLE_WARDEN,
        institution=institution,
        password="TestPassword123!",
    )
    client.force_login(unassigned)
    response = client.get(reverse("accounts:dashboard_warden"))
    assert response.status_code == 200
    html = response.content.decode()

    assert "No hostel has been assigned to your account" in html
    assert "Unassigned" in html
