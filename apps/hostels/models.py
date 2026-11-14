from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.core.models import TimeStampedModel, Institution


class OperationalStatus(models.TextChoices):
    IN_SERVICE = "in_service", "In Service"
    MAINTENANCE = "maintenance", "Under Maintenance"
    OUT_OF_SERVICE = "out_of_service", "Out of Service"


class Hostel(TimeStampedModel):
    """
    Hostel residence within an institution.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MAINTENANCE = "maintenance", "Under Maintenance"
        INACTIVE = "inactive", "Inactive"

    class ResidentCategory(models.TextChoices):
        MALE = "male", "Boys' Hostel"
        FEMALE = "female", "Girls' Hostel"
        POSTGRADUATE = "postgraduate", "Postgraduate Residents"

    class AcademicLevel(models.TextChoices):
        ALL = "all", "All Academic Levels"
        UNDERGRADUATE = "undergraduate", "Undergraduate"
        POSTGRADUATE = "postgraduate", "Postgraduate / Research"

    ACTIVE_RESIDENT_CATEGORIES = (
        (ResidentCategory.MALE, "Boys' Hostel"),
        (ResidentCategory.FEMALE, "Girls' Hostel"),
    )

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="hostels",
        help_text="Owning educational institution",
    )
    name = models.CharField(max_length=255, help_text="Hostel name, e.g. North Residence")
    code = models.CharField(
        max_length=50,
        db_index=True,
        help_text="Unique hostel code within institution, e.g. NR-01",
    )
    resident_category = models.CharField(
        max_length=50,
        choices=ResidentCategory.choices,
        default=ResidentCategory.MALE,
    )
    academic_level = models.CharField(
        max_length=50,
        choices=AcademicLevel.choices,
        default=AcademicLevel.ALL,
        help_text="Academic-level eligibility requirement (independent of residential gender category)",
    )
    address = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    class Meta:
        db_table = "hostels_hostels"
        verbose_name = "Hostel"
        verbose_name_plural = "Hostels"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "code"],
                name="unique_hostel_code_per_institution",
            ),
            models.CheckConstraint(
                condition=~models.Q(resident_category="coed"),
                name="check_hostel_resident_category_not_coed",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def clean(self):
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()
        if self.resident_category == "coed":
            raise ValidationError({"resident_category": "Co-ed hostels are not supported. Choose Boys' Hostel or Girls' Hostel."})
        if self.resident_category not in [self.ResidentCategory.MALE, self.ResidentCategory.FEMALE, self.ResidentCategory.POSTGRADUATE]:
            raise ValidationError({"resident_category": f"Invalid resident category: '{self.resident_category}'."})

    @property
    def occupancy_rate(self) -> float:
        if hasattr(self, "_occupancy_rate") and self._occupancy_rate is not None:
            return self._occupancy_rate
        total = getattr(self, "total_beds", None)
        if total is None:
            total = Bed.objects.filter(room__floor__block__hostel=self, is_active=True).count()
        occupied = getattr(self, "occupied_beds", None)
        if occupied is None:
            occupied = StudentOccupancy.objects.filter(
                bed__room__floor__block__hostel=self,
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).count()
        if total and total > 0:
            return round((occupied / total) * 100, 1)
        return 0.0

    @occupancy_rate.setter
    def occupancy_rate(self, value: float):
        self._occupancy_rate = value


class Block(TimeStampedModel):
    """Building block or wing within a hostel."""

    hostel = models.ForeignKey(
        Hostel,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    name = models.CharField(max_length=100, help_text="e.g. Block A, East Wing")
    code = models.CharField(max_length=50, help_text="e.g. BLK-A")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "hostels_blocks"
        verbose_name = "Block"
        verbose_name_plural = "Blocks"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["hostel", "code"],
                name="unique_block_code_per_hostel",
            )
        ]

    def __str__(self):
        return f"{self.hostel.name} - {self.name}"

    def clean(self):
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()


class Floor(TimeStampedModel):
    """Floor level inside a block."""

    block = models.ForeignKey(
        Block,
        on_delete=models.CASCADE,
        related_name="floors",
    )
    floor_number = models.IntegerField(help_text="Floor numeric index, e.g. 0 for Ground, 1 for 1st")
    floor_label = models.CharField(max_length=100, help_text="e.g. Ground Floor, 1st Floor")
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "hostels_floors"
        verbose_name = "Floor"
        verbose_name_plural = "Floors"
        ordering = ["block", "floor_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["block", "floor_number"],
                name="unique_floor_number_per_block",
            )
        ]

    def __str__(self):
        return f"{self.block} - {self.floor_label}"


class Room(TimeStampedModel):
    """Hostel room located on a specific floor."""

    class RoomType(models.TextChoices):
        SINGLE = "single", "Single Sharing (1 Bed)"
        DOUBLE = "double", "Double Sharing (2 Beds)"
        TRIPLE = "triple", "Triple Sharing (3 Beds)"
        FOUR = "four", "Four Sharing (4 Beds)"
        FIVE = "five", "Five Sharing (5 Beds)"
        SIX = "six", "Six Sharing (6 Beds)"
        DORMITORY = "dormitory", "Dormitory / Multi-Bed"
        CUSTOM = "custom", "Custom Layout"

    ROOM_TYPE_CAPACITY = {
        RoomType.SINGLE: 1,
        RoomType.DOUBLE: 2,
        RoomType.TRIPLE: 3,
        RoomType.FOUR: 4,
        RoomType.FIVE: 5,
        RoomType.SIX: 6,
        RoomType.DORMITORY: 8,
        RoomType.CUSTOM: 1,
    }

    floor = models.ForeignKey(
        Floor,
        on_delete=models.CASCADE,
        related_name="rooms",
    )
    room_number = models.CharField(max_length=50, db_index=True, help_text="e.g. 101, 204")
    room_type = models.CharField(
        max_length=20,
        choices=RoomType.choices,
        default=RoomType.DOUBLE,
    )
    occupancy_category = models.CharField(
        max_length=100,
        default="Standard",
        help_text="e.g. Standard, AC, Attached Bath",
    )
    accessibility_attributes = models.CharField(
        max_length=255,
        blank=True,
        help_text="e.g. Wheelchair accessible, Near elevator",
    )
    operational_status = models.CharField(
        max_length=30,
        choices=OperationalStatus.choices,
        default=OperationalStatus.IN_SERVICE,
        db_index=True,
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "hostels_rooms"
        verbose_name = "Room"
        verbose_name_plural = "Rooms"
        ordering = ["floor", "room_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["floor", "room_number"],
                name="unique_room_number_per_floor",
            )
        ]

    def __str__(self):
        return f"Room {self.room_number} ({self.floor.block.hostel.code})"

    @property
    def target_capacity(self) -> int:
        if self.pk:
            count = self.beds.filter(is_active=True).count()
            if count > 0:
                return count
        return self.ROOM_TYPE_CAPACITY.get(self.room_type, 1)

    @property
    def hostel(self) -> Hostel:
        return self.floor.block.hostel


class Bed(TimeStampedModel):
    """Individual bed slot in a room."""

    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        related_name="beds",
    )
    bed_label = models.CharField(max_length=50, help_text="e.g. Bed A, Bed B, Bed 1")
    operational_status = models.CharField(
        max_length=30,
        choices=OperationalStatus.choices,
        default=OperationalStatus.IN_SERVICE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "hostels_beds"
        verbose_name = "Bed"
        verbose_name_plural = "Beds"
        ordering = ["room", "bed_label"]
        constraints = [
            models.UniqueConstraint(
                fields=["room", "bed_label"],
                name="unique_bed_label_per_room",
            )
        ]

    def __str__(self):
        return f"{self.room.room_number} - {self.bed_label}"

    @property
    def is_occupied(self) -> bool:
        """Derived property checking whether an active allocation currently exists."""
        return self.occupancies.filter(status=StudentOccupancy.OccupancyStatus.ACTIVE).exists()

    @property
    def is_available_for_allocation(self) -> bool:
        """Available only if operational status is IN_SERVICE, is_active is True, and has no active occupant."""
        if not self.is_active or self.operational_status != OperationalStatus.IN_SERVICE:
            return False
        if not self.room.is_active or self.room.operational_status != OperationalStatus.IN_SERVICE:
            return False
        if not self.room.floor.is_active or not self.room.floor.block.is_active:
            return False
        if self.room.floor.block.hostel.status != Hostel.Status.ACTIVE:
            return False
        return not self.is_occupied


class WardenAssignment(TimeStampedModel):
    """Authorized warden assignment linking staff to one or more hostels."""

    warden = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="warden_assignments",
    )
    hostel = models.ForeignKey(
        Hostel,
        on_delete=models.CASCADE,
        related_name="assigned_wardens",
    )
    is_primary = models.BooleanField(default=False)
    assigned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "hostels_warden_assignments"
        verbose_name = "Warden Assignment"
        verbose_name_plural = "Warden Assignments"
        constraints = [
            models.UniqueConstraint(
                fields=["warden", "hostel"],
                name="unique_warden_per_hostel",
            )
        ]

    def __str__(self):
        return f"{self.warden.full_name} -> {self.hostel.name}"


class StudentOccupancy(TimeStampedModel):
    """
    Student-to-bed allocation record.
    Preserves full audit trail and historical residency.
    Strict database constraints ensure:
    1. A student can have only ONE active bed allocation.
    2. A bed can have only ONE active student.
    3. End date must be >= start date.
    """

    class OccupancyStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        TRANSFERRED = "transferred", "Transferred"
        RELEASED = "released", "Released"
        CANCELLED = "cancelled", "Cancelled"

    class AllocationSource(models.TextChoices):
        ADMINISTRATIVE = "administrative", "Administrative Allocation"
        DIRECT_SWAP = "direct_swap", "Direct 1-to-1 Exchange"
        MULTI_SWAP = "multi_swap", "Multi-Party Swap Ring"
        SEED = "seed", "Initial System Seed"

    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="occupancies",
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="occupancy_records",
    )
    bed = models.ForeignKey(
        Bed,
        on_delete=models.PROTECT,
        related_name="occupancies",
    )
    start_date = models.DateField(default=timezone.now, db_index=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=30,
        choices=OccupancyStatus.choices,
        default=OccupancyStatus.ACTIVE,
        db_index=True,
    )
    source = models.CharField(
        max_length=30,
        choices=AllocationSource.choices,
        default=AllocationSource.ADMINISTRATIVE,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_allocations",
    )
    release_reason = models.TextField(blank=True)

    class Meta:
        db_table = "hostels_student_occupancies"
        verbose_name = "Student Occupancy"
        verbose_name_plural = "Student Occupancies"
        ordering = ["-start_date", "-created_at"]
        indexes = [
            models.Index(fields=["status", "student"]),
            models.Index(fields=["status", "bed"]),
            models.Index(fields=["institution", "status"]),
        ]
        constraints = [
            # Constraint 1: A student can only have ONE active allocation
            models.UniqueConstraint(
                fields=["student"],
                condition=models.Q(status="active"),
                name="unique_active_occupancy_per_student",
            ),
            # Constraint 2: A bed can only have ONE active student
            models.UniqueConstraint(
                fields=["bed"],
                condition=models.Q(status="active"),
                name="unique_active_occupancy_per_bed",
            ),
            # Constraint 3: End date must not precede start date
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True) | models.Q(end_date__gte=models.F("start_date")),
                name="occupancy_end_date_gte_start_date",
            ),
        ]

    def __str__(self):
        return f"{self.student.full_name} in {self.bed} [{self.status}]"

    def clean(self):
        super().clean()
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot precede allocation start date."})

        # Institution scoping validation
        if self.student and self.student.institution_id != self.institution_id:
            raise ValidationError("Student does not belong to the allocation's institution.")
        if self.bed and self.bed.room.floor.block.hostel.institution_id != self.institution_id:
            raise ValidationError("Bed does not belong to the allocation's institution.")
