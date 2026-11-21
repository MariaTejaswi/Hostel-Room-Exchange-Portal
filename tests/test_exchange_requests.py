import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.exchange.eligibility import UnavailableExternalEligibilityAdapter
from apps.exchange.models import ExchangeRequest, PolicyRule
from apps.exchange.services import (
    create_exchange_request,
    update_exchange_request,
    withdraw_exchange_request,
)
from apps.hostels.services import assign_student_to_bed


def _request_data(**overrides):
    values = {
        "preferred_hostel": None,
        "preferred_block": None,
        "preferred_floor": None,
        "preferred_room_type": "",
        "preferred_occupancy_category": "",
        "preferred_accessibility_attributes": "",
        "reason": "I need accommodation closer to my academic facilities.",
    }
    values.update(overrides)
    return values


def _occupy(student_user, hostel_setup, warden_user, **kwargs):
    return assign_student_to_bed(
        student=student_user,
        bed=hostel_setup["bed_a"],
        created_by=warden_user,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def configured_policies(institution):
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.COOLING_OFF_DAYS,
        defaults={"value": {"value": 0}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.REQUEST_VALIDITY_DAYS,
        defaults={"value": {"value": 30}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
        defaults={"value": {"value": False}, "is_active": True},
    )
    PolicyRule.objects.update_or_create(
        institution=institution,
        key=PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD,
        defaults={"value": {"value": False}, "is_active": True},
    )



@pytest.mark.django_db
def test_student_can_create_exchange_request(client, student_user, hostel_setup, warden_user):
    _occupy(student_user, hostel_setup, warden_user)
    client.force_login(student_user)
    post_data = _request_data(preferred_hostel=str(hostel_setup["hostel"].pk))
    post_data.pop("preferred_block")
    post_data.pop("preferred_floor")

    response = client.post(
        reverse("exchange:request_create"),
        post_data,
    )

    assert response.status_code == 302, response.context["form"].errors
    exchange_request = ExchangeRequest.objects.get(student=student_user)
    assert exchange_request.status == ExchangeRequest.Status.OPEN
    assert exchange_request.current_occupancy.student_id == student_user.id


@pytest.mark.django_db
def test_student_can_select_an_occupied_bed_as_exchange_target(
    student_user,
    other_student_user,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    assign_student_to_bed(
        student=other_student_user,
        bed=hostel_setup["bed_b"],
        created_by=warden_user,
    )

    exchange_request = create_exchange_request(
        student=student_user,
        data=_request_data(preferred_bed=hostel_setup["bed_b"]),
    )

    assert exchange_request.preferred_bed_id == hostel_setup["bed_b"].id


@pytest.mark.django_db
def test_maintenance_bed_cannot_be_selected_as_exchange_target(
    student_user,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    hostel_setup["bed_b"].operational_status = "maintenance"
    hostel_setup["bed_b"].save(update_fields=["operational_status"])

    with pytest.raises(ValidationError, match="Preferred bed is not available"):
        create_exchange_request(
            student=student_user,
            data=_request_data(preferred_bed=hostel_setup["bed_b"]),
        )


@pytest.mark.django_db
def test_request_form_persists_visual_bed_selection(
    client,
    student_user,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    client.force_login(student_user)

    response = client.post(
        reverse("exchange:request_create"),
        {
            "preferred_bed": str(hostel_setup["bed_b"].id),
            "preferred_room_type": "",
            "preferred_occupancy_category": "",
            "preferred_accessibility_attributes": "",
            "reason": "I need this selected room for my academic schedule.",
        },
    )

    assert response.status_code == 302
    assert ExchangeRequest.objects.get(student=student_user).preferred_bed_id == hostel_setup["bed_b"].id


@pytest.mark.django_db
def test_request_form_loads_database_beds_and_occupancy_state(
    client,
    student_user,
    other_student_user,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    assign_student_to_bed(
        student=other_student_user,
        bed=hostel_setup["bed_b"],
        created_by=warden_user,
    )
    client.force_login(student_user)

    response = client.get(reverse("exchange:request_create"))

    assert response.status_code == 200
    assert b"Bed A" in response.content
    assert b"Bed B" in response.content
    assert b"Occupied" in response.content


@pytest.mark.django_db
def test_unverified_residential_eligibility_is_rejected(
    student_user,
    hostel_setup,
    warden_user,
):
    occupancy = _occupy(student_user, hostel_setup, warden_user)
    student_user.residential_eligibility = "unverified"
    student_user.save(update_fields=["residential_eligibility"])

    with pytest.raises(ValidationError, match="residential eligibility"):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
        )

    assert not ExchangeRequest.objects.filter(student=student_user).exists()
    assert occupancy.status == "active"


@pytest.mark.django_db
def test_request_requires_an_active_occupancy(student_user):
    with pytest.raises(ValidationError, match="active room allocation"):
        create_exchange_request(student=student_user, data=_request_data())


@pytest.mark.django_db
def test_cooling_off_and_external_status_policies_are_enforced(
    student_user,
    institution,
    hostel_setup,
    warden_user,
):
    _occupy(
        student_user,
        hostel_setup,
        warden_user,
        start_date=timezone.localdate(),
    )
    PolicyRule.objects.filter(
        institution=institution,
        key=PolicyRule.Key.COOLING_OFF_DAYS,
    ).update(value={"value": 2})
    with pytest.raises(ValidationError, match="cooling-off"):
        create_exchange_request(student=student_user, data=_request_data())

    PolicyRule.objects.filter(
        institution=institution,
        key=PolicyRule.Key.COOLING_OFF_DAYS,
    ).update(value={"value": 0})
    PolicyRule.objects.filter(
        institution=institution,
        key=PolicyRule.Key.REQUIRE_FEE_CLEARANCE,
    ).update(value={"value": True})
    with pytest.raises(ValidationError, match="Fee status could not be verified"):
        create_exchange_request(
            student=student_user,
            data=_request_data(),
            status_adapter=UnavailableExternalEligibilityAdapter(),
        )


@pytest.mark.django_db
def test_duplicate_active_request_is_rejected(student_user, hostel_setup, warden_user):
    _occupy(student_user, hostel_setup, warden_user)
    create_exchange_request(student=student_user, data=_request_data())

    with pytest.raises(ValidationError, match="already have an active"):
        create_exchange_request(student=student_user, data=_request_data())


@pytest.mark.django_db
def test_student_can_edit_and_withdraw_owned_request(
    student_user,
    hostel_setup,
    warden_user,
):
    _occupy(student_user, hostel_setup, warden_user)
    exchange_request = create_exchange_request(
        student=student_user,
        data=_request_data(),
    )

    updated = update_exchange_request(
        exchange_request=exchange_request,
        student=student_user,
        data=_request_data(reason="I need a quieter room for accessibility reasons."),
    )
    assert updated.reason.startswith("I need a quieter")

    withdrawn = withdraw_exchange_request(
        exchange_request=updated,
        student=student_user,
    )
    assert withdrawn.status == ExchangeRequest.Status.WITHDRAWN


@pytest.mark.django_db
def test_request_access_is_owner_scoped_and_staff_cannot_manage(
    client,
    student_user,
    other_student_user,
    warden_user,
    hostel_setup,
):
    _occupy(student_user, hostel_setup, warden_user)
    exchange_request = create_exchange_request(
        student=student_user,
        data=_request_data(),
    )

    client.force_login(other_student_user)
    assert client.get(
        reverse("exchange:request_detail", kwargs={"pk": exchange_request.pk})
    ).status_code == 404

    client.force_login(warden_user)
    assert client.get(reverse("exchange:request_list")).status_code == 403


@pytest.mark.django_db
def test_exchange_request_submission_redirects_and_displays_success_message(
    client, student_user, hostel_setup, warden_user
):
    """
    Submitting an exchange request must validate, save to DB, set a success message,
    and redirect immediately to 'My Exchange Requests' without hanging.
    """
    _occupy(student_user, hostel_setup, warden_user)
    client.force_login(student_user)

    post_data = _request_data(
        preferred_hostel=str(hostel_setup["hostel"].pk),
        reason="Requesting room closer to campus center for morning laboratory classes.",
    )
    post_data.pop("preferred_block")
    post_data.pop("preferred_floor")

    response = client.post(
        reverse("exchange:request_create"),
        post_data,
        follow=True,
    )

    # 1. Successful redirect occurred
    assert response.status_code == 200
    assert response.redirect_chain == [(reverse("exchange:request_list"), 302)]

    # 2. Success message is delivered
    messages = list(response.context["messages"])
    assert any("Your exchange request was created." in str(m) for m in messages)

    # 3. Newly created request is present in DB and page context
    created_request = ExchangeRequest.objects.get(student=student_user)
    assert created_request.status == ExchangeRequest.Status.OPEN
    assert created_request.reason.startswith("Requesting room closer")


@pytest.mark.django_db
def test_exchange_request_broker_failure_resilience_and_fail_fast(
    client, student_user, hostel_setup, warden_user
):
    """
    When the Celery broker is unreachable (e.g. local dev without Redis),
    schedule_institution_matching must fail-fast without blocking the HTTP request thread,
    persist the MatchingOutbox record, and return the 302 redirect seamlessly.
    """
    import time
    from unittest.mock import patch
    from kombu.exceptions import OperationalError
    from apps.exchange.models import MatchingOutbox

    _occupy(student_user, hostel_setup, warden_user)
    client.force_login(student_user)

    post_data = _request_data(
        preferred_hostel=str(hostel_setup["hostel"].pk),
        reason="Testing fail-fast broker resilience under simulated broker outage.",
    )
    post_data.pop("preferred_block")
    post_data.pop("preferred_floor")

    # Simulate Celery broker being completely unavailable / connection refused
    with patch("config.celery.app.connection_for_write", side_effect=OperationalError("Connection refused")):
        t_start = time.time()
        response = client.post(
            reverse("exchange:request_create"),
            post_data,
        )
        elapsed = time.time() - t_start

    # Must complete almost instantaneously (under 1 second) and redirect
    assert elapsed < 1.0, f"Request took too long ({elapsed}s) during broker outage"
    assert response.status_code == 302
    assert response["Location"] == reverse("exchange:request_list")

    # Request and durable outbox record must both exist
    assert ExchangeRequest.objects.filter(student=student_user).exists()
    assert MatchingOutbox.objects.filter(
        institution=student_user.institution,
        event_type="request_created",
    ).exists()
