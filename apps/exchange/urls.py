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
    path(
        "exchange/policies/",
        views.exchange_policy_settings_view,
        name="policy_settings",
    ),
    # Lightweight AJAX endpoints for dynamic bed/room explorer
    path(
        "exchange/api/hostels/<str:hostel_id>/blocks/",
        views.api_exchange_blocks,
        name="api_blocks",
    ),
    path(
        "exchange/api/blocks/<str:block_id>/floors/",
        views.api_exchange_floors,
        name="api_floors",
    ),
    path(
        "exchange/api/floors/<str:floor_id>/rooms/",
        views.api_exchange_floor_rooms,
        name="api_floor_rooms",
    ),
    path(
        "exchange/api/rooms/<str:room_id>/beds/",
        views.api_exchange_room_beds,
        name="api_room_beds",
    ),
]
