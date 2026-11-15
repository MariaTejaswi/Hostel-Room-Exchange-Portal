from django.urls import path

from . import views

app_name = "exchange"

urlpatterns = [
    path("exchange/requests/", views.exchange_request_list_view, name="request_list"),
    path(
        "exchange/notifications/",
        views.exchange_notifications_view,
        name="notifications",
    ),
    path(
        "exchange/requests/create/",
        views.exchange_request_create_view,
        name="request_create",
    ),
    path(
        "exchange/requests/<uuid:pk>/",
        views.exchange_request_detail_view,
        name="request_detail",
    ),
    path(
        "exchange/requests/<uuid:pk>/edit/",
        views.exchange_request_edit_view,
        name="request_edit",
    ),
    path(
        "exchange/requests/<uuid:pk>/withdraw/",
        views.exchange_request_withdraw_view,
        name="request_withdraw",
    ),
]
