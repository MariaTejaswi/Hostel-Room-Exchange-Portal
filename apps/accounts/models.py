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

    class ClaimedResidentialCategory(models.TextChoices):
        MALE = "male", "Boys' Hostel"
        FEMALE = "female", "Girls' Hostel"
        UNSPECIFIED = "unspecified", "Unspecified / Not Disclosed"

    class AcademicYear(models.TextChoices):
        YEAR_1 = "1st Year", "1st Year"
        YEAR_2 = "2nd Year", "2nd Year"
        YEAR_3 = "3rd Year", "3rd Year"
        YEAR_4 = "4th Year", "4th Year"
        YEAR_5 = "5th Year", "5th Year"
        POSTGRAD = "Post-Graduate", "Post-Graduate"
        RESEARCH = "Research Scholar", "Research Scholar"
        OTHER = "Other", "Other"

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
    department = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Academic department or faculty",
    )
    program = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Academic degree or study program",
    )
    academic_year = models.CharField(
        max_length=50,
        blank=True,
        default="",
        choices=AcademicYear.choices,
        help_text="Current academic year level",
    )
    phone_number = models.CharField(max_length=20, blank=True, help_text="Primary phone number")
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    claimed_residential_category = models.CharField(
        max_length=20,
        choices=ClaimedResidentialCategory.choices,
        default=ClaimedResidentialCategory.UNSPECIFIED,
        blank=True,
        help_text="Self-reported residential preference (unverified)",
    )
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
        if hasattr(self, "_cached_primary_role"):
            return self._cached_primary_role
        if hasattr(self, "_prefetched_objects_cache") and "groups" in self._prefetched_objects_cache:
            group_names = {g.name for g in self.groups.all()}
        else:
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
                self._cached_primary_role = role
                return role
        if self.is_superuser:
            role = "System Administrator"
        else:
            role = "Student"
        self._cached_primary_role = role
        return role

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

    @property
    def current_occupancy(self):
        """Return active StudentOccupancy if any, or None."""
        if hasattr(self, "_prefetched_objects_cache") and "occupancy_records" in self._prefetched_objects_cache:
            for occ in self.occupancy_records.all():
                if occ.status == "active":
                    return occ
            return None
        if hasattr(self, "occupancy_records"):
            return self.occupancy_records.filter(status="active").select_related(
                "bed__room__floor__block__hostel"
            ).first()
        from apps.hostels.models import StudentOccupancy
        return StudentOccupancy.objects.filter(
            student=self,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        ).select_related("bed__room__floor__block__hostel").first()


class EligibilityVerificationRecord(TimeStampedModel):
    """
    Audit trail of authoritative residential eligibility determinations made by Hostel Administrators.
    Preserves history of each verification event, previous state, new state, verifier, and justification.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="eligibility_verifications",
        help_text="Student whose eligibility was reviewed",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="eligibility_verifications",
        help_text="Institution context",
    )
    previous_eligibility = models.CharField(
        max_length=20,
        choices=User.ResidentialEligibility.choices,
        help_text="Eligibility status prior to this action",
    )
    new_eligibility = models.CharField(
        max_length=20,
        choices=User.ResidentialEligibility.choices,
        help_text="Authoritative eligibility set by verifier",
    )
    verifier = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="verified_eligibilities",
        help_text="Hostel Administrator who performed the verification",
    )
    reason = models.TextField(
        help_text="Official verification reference, record citation, or justification note",
    )

    class Meta:
        db_table = "accounts_eligibility_verifications"
        verbose_name = "Eligibility Verification Record"
        verbose_name_plural = "Eligibility Verification Records"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.student.full_name}: {self.previous_eligibility} -> {self.new_eligibility} by {self.verifier.full_name}"
