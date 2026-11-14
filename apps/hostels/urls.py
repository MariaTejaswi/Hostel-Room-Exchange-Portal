from django.urls import path
from . import views

app_name = "hostels"

urlpatterns = [
    # Hostels
    path("hostels/", views.hostel_list_view, name="hostel_list"),
    path("hostels/create/", views.hostel_create_view, name="hostel_create"),
    path("hostels/builder/", views.hostel_builder_view, name="hostel_builder"),
    path("hostels/<int:pk>/", views.hostel_detail_view, name="hostel_detail"),
    path("hostels/<int:pk>/edit/", views.hostel_edit_view, name="hostel_edit"),
    path("hostels/<int:pk>/builder/", views.hostel_builder_view, name="hostel_edit_builder"),
    # Rooms
    path("rooms/", views.room_inventory_view, name="room_inventory"),
    path("rooms/create/", views.room_create_view, name="room_create"),
    path("rooms/<int:pk>/", views.room_detail_view, name="room_detail"),
    # Beds
    path("beds/", views.bed_list_view, name="bed_list"),
    path("beds/<int:pk>/status/", views.bed_status_update_view, name="bed_status_update"),
    # Occupancy
    path("occupancy/", views.occupancy_list_view, name="occupancy_list"),
    path("occupancy/assign/", views.occupancy_assign_view, name="occupancy_assign"),
    path("occupancy/<int:pk>/", views.occupancy_detail_view, name="occupancy_detail"),
    path("occupancy/<int:pk>/release/", views.occupancy_release_view, name="occupancy_release"),
]
