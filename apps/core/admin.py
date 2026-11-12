from django.contrib import admin
from .models import Institution, AuditLog


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "city", "state", "contact_email", "is_active", "created_at")
    list_filter = ("is_active", "state")
    search_fields = ("name", "code", "contact_email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "acting_user", "institution", "action", "target_type", "target_id", "ip_address")
    list_filter = ("action", "target_type", "timestamp")
    search_fields = ("acting_user__email", "target_id", "change_summary")
    readonly_fields = (
        "acting_user",
        "institution",
        "action",
        "target_type",
        "target_id",
        "timestamp",
        "change_summary",
        "ip_address",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
