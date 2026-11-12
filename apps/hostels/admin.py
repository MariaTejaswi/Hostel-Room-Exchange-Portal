from django.contrib import admin
from .models import (
    Hostel,
    Block,
    Floor,
    Room,
    Bed,
    WardenAssignment,
    StudentOccupancy,
)


class BlockInline(admin.TabularInline):
    model = Block
    extra = 1


class FloorInline(admin.TabularInline):
    model = Floor
    extra = 1


class BedInline(admin.TabularInline):
    model = Bed
    extra = 1


@admin.register(Hostel)
class HostelAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "institution", "resident_category", "status", "created_at")
    list_filter = ("status", "resident_category", "institution")
    search_fields = ("name", "code", "address")
    inlines = [BlockInline]


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "hostel", "created_at")
    list_filter = ("hostel__institution", "hostel")
    search_fields = ("name", "code", "hostel__name")
    inlines = [FloorInline]


@admin.register(Floor)
class FloorAdmin(admin.ModelAdmin):
    list_display = ("floor_label", "floor_number", "block", "created_at")
    list_filter = ("block__hostel",)
    search_fields = ("floor_label", "block__name")


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("room_number", "floor", "room_type", "occupancy_category", "operational_status")
    list_filter = ("operational_status", "room_type", "floor__block__hostel")
    search_fields = ("room_number", "floor__block__name", "floor__block__hostel__name")
    inlines = [BedInline]


@admin.register(Bed)
class BedAdmin(admin.ModelAdmin):
    list_display = ("bed_label", "room", "operational_status", "is_occupied", "created_at")
    list_filter = ("operational_status", "room__floor__block__hostel")
    search_fields = ("bed_label", "room__room_number")


@admin.register(WardenAssignment)
class WardenAssignmentAdmin(admin.ModelAdmin):
    list_display = ("warden", "hostel", "is_primary", "assigned_at")
    list_filter = ("is_primary", "hostel")
    search_fields = ("warden__email", "warden__full_name", "hostel__name")


@admin.register(StudentOccupancy)
class StudentOccupancyAdmin(admin.ModelAdmin):
    list_display = ("student", "bed", "status", "start_date", "end_date", "source", "created_at")
    list_filter = ("status", "source", "institution")
    search_fields = ("student__email", "student__full_name", "student__registration_number", "bed__room__room_number")
    readonly_fields = ("created_at", "updated_at")
