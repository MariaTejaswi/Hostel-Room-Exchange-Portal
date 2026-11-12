from django.db import models
from django.conf import settings


class TimeStampedModel(models.Model):
    """Abstract base model that provides self-updating created_at and updated_at fields."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Institution(TimeStampedModel):
    """
    University or Educational Institution.
    All hostels and user records are scoped under an institution.
    """

    name = models.CharField(max_length=255, help_text="Full institution name")
    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Unique institution identifier code, e.g. UNIV-DEMO",
    )
    address = models.CharField(max_length=500, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    contact_email = models.EmailField(help_text="Institutional administrative contact email")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "core_institutions"
        verbose_name = "Institution"
        verbose_name_plural = "Institutions"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class AuditLog(models.Model):
    """
    Immutable audit log for privileged operations across the platform.
    Tracks acting user, target entity, timestamp, action type, and change summary.
    """

    acting_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Action performed, e.g. OCCUPANCY_ASSIGNED, OCCUPANCY_RELEASED",
    )
    target_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Entity type modified, e.g. StudentOccupancy, Room, Bed",
    )
    target_id = models.CharField(max_length=100, help_text="Identifier of the target entity")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    change_summary = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "core_audit_logs"
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ["-timestamp"]

    def __str__(self):
        user_display = self.acting_user.email if self.acting_user else "System"
        return f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] {user_display} -> {self.action} on {self.target_type}:{self.target_id}"
