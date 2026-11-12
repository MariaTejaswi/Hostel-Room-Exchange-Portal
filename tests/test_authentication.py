import pytest
from django.urls import reverse
from apps.accounts.models import User


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
