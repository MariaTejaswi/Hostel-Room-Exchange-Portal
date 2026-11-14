import uuid
from django.db import models
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
    Group,
)
from django.utils import timezone
from apps.core.models import Institution, TimeStampedModel


class UserManager(BaseUserManager):
    """Custom user manager supporting email-based authentication and institution scoping."""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be provided.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("account_status", User.AccountStatus.ACTIVE)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    Custom user model for RoomLoop.
    Uses UUID primary key, email as username, and links to an Institution.
    Role assignment is strictly managed via Django Groups.
    """

    class AccountStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        PENDING = "pending", "Pending Verification"
        SUSPENDED = "suspended", "Suspended"
        INACTIVE = "inactive", "Inactive"

    class ResidentialEligibility(models.TextChoices):
        MALE = "male", "Boys' Hostel Eligible"
        FEMALE = "female", "Girls' Hostel Eligible"
        UNVERIFIED = "unverified", "Pending Institutional Verification"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(
        unique=True,
        max_length=255,
        db_index=True,
        help_text="Institutional or official email address",
    )
    full_name = models.CharField(max_length=255, help_text="Full legal name")
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
        help_text="Associated institution (required for non-global staff)",
    )
    registration_number = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        db_index=True,
        help_text="Student ID / Roll / Registration number (for students)",
    )
    phone_number = models.CharField(max_length=20, blank=True, help_text="Primary phone number")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    residential_eligibility = models.CharField(
        max_length=20,
        choices=ResidentialEligibility.choices,
        default=ResidentialEligibility.UNVERIFIED,
        db_index=True,
        help_text="Authoritative verified residential eligibility (set by Registrar / Student Welfare)",
    )
    account_status = models.CharField(
        max_length=20,
        choices=AccountStatus.choices,
        default=AccountStatus.ACTIVE,
        db_index=True,
    )
    is_staff = models.BooleanField(
        default=False,
        help_text="Designates whether the user can log into the Django admin site.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Designates whether this user account should be treated as active.",
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "accounts_users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["full_name", "email"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "registration_number"],
                condition=models.Q(registration_number__isnull=False) & ~models.Q(registration_number=""),
                name="unique_registration_per_institution",
            )
        ]

    def __str__(self):
        reg = f" ({self.registration_number})" if self.registration_number else ""
        return f"{self.full_name} <{self.email}>{reg}"

    def clean(self):
        super().clean()
        if self.email:
            self.email = self.email.strip().lower()
        if self.registration_number:
            self.registration_number = self.registration_number.strip().upper()

    @property
    def primary_role(self) -> str:
        """Return the primary role name based on group membership or superuser status."""
        group_names = set(self.groups.values_list("name", flat=True))
        for role in [
            "Dean of Student Welfare",
            "Hostel Administrator",
            "Chief Warden",
            "Warden",
            "System Administrator",
            "Student",
        ]:
            if role in group_names:
                return role
        if self.is_superuser:
            return "System Administrator"
        return "Student"

    @property
    def is_student_role(self) -> bool:
        return self.primary_role == "Student"

    @property
    def is_warden_role(self) -> bool:
        return self.primary_role == "Warden"

    @property
    def is_chief_warden_role(self) -> bool:
        return self.primary_role == "Chief Warden"

    @property
    def is_hostel_admin_role(self) -> bool:
        return self.primary_role == "Hostel Administrator"

    @property
    def is_dean_role(self) -> bool:
        return self.primary_role == "Dean of Student Welfare"

    @property
    def is_sysadmin_role(self) -> bool:
        return self.primary_role == "System Administrator"
