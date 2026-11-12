from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "email",
        "full_name",
        "institution",
        "registration_number",
        "primary_role",
        "account_status",
        "is_active",
        "is_staff",
    )
    list_filter = ("account_status", "is_active", "is_staff", "institution", "groups")
    search_fields = ("email", "full_name", "registration_number", "phone_number")
    ordering = ("email",)

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Personal Information",
            {"fields": ("full_name", "institution", "registration_number", "phone_number", "avatar")},
        ),
        (
            "Permissions & Status",
            {
                "fields": (
                    "account_status",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined", "created_at", "updated_at")}),
    )

    readonly_fields = ("date_joined", "last_login", "created_at", "updated_at")
