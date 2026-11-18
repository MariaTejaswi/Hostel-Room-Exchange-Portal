import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import Institution, TimeStampedModel
from apps.hostels.models import (
    Bed,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
)


class ExchangeRequest(TimeStampedModel):
    """A student's request to exchange their current accommodation."""

    class RequestType(models.TextChoices):
        EXCHANGE = "exchange", "Occupied-bed Exchange"
        VACANT_TRANSFER = "vacant_transfer", "Vacant-bed Transfer"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        MATCHED = "matched", "Matched"
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"
        EXPIRED = "expired", "Expired"
        COMPLETED = "completed", "Completed"

    class MatchingStatus(models.TextChoices):
        WAITING = "waiting", "Waiting for Matching"
        PROCESSING = "processing", "Matching in Progress"
        NO_MATCH = "no_match", "No Compatible Match Found"
        PROPOSAL_AVAILABLE = "proposal_available", "Proposal Available"
        UNAVAILABLE = "unavailable", "Matching Temporarily Unavailable"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="exchange_requests",
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="exchange_requests",
    )
    current_occupancy = models.ForeignKey(
        StudentOccupancy,
        on_delete=models.PROTECT,
        related_name="exchange_requests",
    )
    request_type = models.CharField(
        max_length=20,
        choices=RequestType.choices,
        default=RequestType.EXCHANGE,
        db_index=True,
    )
    preferred_bed = models.ForeignKey(
        Bed,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_exchange_requests",
        help_text="Optional exact bed selected as the exchange target.",
    )
    preferred_hostel = models.ForeignKey(
        Hostel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_exchange_requests",
    )
    preferred_block = models.ForeignKey(
        "hostels.Block",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_exchange_requests",
    )
    preferred_floor = models.ForeignKey(
        "hostels.Floor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_exchange_requests",
    )
    preferred_room_type = models.CharField(
        max_length=20,
        choices=Room.RoomType.choices,
        blank=True,
    )
    preferred_occupancy_category = models.CharField(max_length=100, blank=True)
    preferred_accessibility_attributes = models.CharField(max_length=255, blank=True)
    reason = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    matching_status = models.CharField(
        max_length=25,
        choices=MatchingStatus.choices,
        default=MatchingStatus.WAITING,
        db_index=True,
    )
    last_matched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    matching_failure_reason = models.TextField(blank=True)
    preference_updated_at = models.DateTimeField(default=timezone.now, db_index=True)
    submitted_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "exchange_requests"
        ordering = ["-submitted_at", "-created_at"]
        indexes = [
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["institution", "request_type", "status"]),
            models.Index(fields=["student", "status"]),
            models.Index(fields=["expires_at", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(status__in=["open", "matched", "pending_approval"]),
                name="unique_active_exchange_request_per_student",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gte=models.F("created_at")),
                name="exchange_request_expiry_after_creation",
            ),
        ]

    def __str__(self):
        return f"{self.student.full_name} {self.get_request_type_display()} ({self.status})"

    def clean(self):
        super().clean()
        if self.request_type == self.RequestType.VACANT_TRANSFER and not self.preferred_bed_id:
            raise ValidationError("A specific preferred bed is required for a vacant-bed transfer.")
        if self.student_id and self.institution_id and self.student.institution_id != self.institution_id:
            raise ValidationError("Student and exchange request must belong to the same institution.")
        if self.current_occupancy_id:
            if self.current_occupancy.student_id != self.student_id:
                raise ValidationError("The current occupancy must belong to the requesting student.")
            if self.current_occupancy.institution_id != self.institution_id:
                raise ValidationError("Current occupancy and exchange request must share an institution.")
            if self.current_occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE:
                raise ValidationError("Exchange requests require an active current occupancy.")
        if self.preferred_bed_id:
            target_hostel = self.preferred_bed.room.floor.block.hostel
            if target_hostel.institution_id != self.institution_id:
                raise ValidationError("Preferred bed must belong to the request institution.")
            if (
                not self.preferred_bed.is_active
                or self.preferred_bed.operational_status != OperationalStatus.IN_SERVICE
                or not self.preferred_bed.room.is_active
                or self.preferred_bed.room.operational_status != "in_service"
                or not self.preferred_bed.room.floor.is_active
                or not self.preferred_bed.room.floor.block.is_active
                or target_hostel.status != Hostel.Status.ACTIVE
            ):
                raise ValidationError("Preferred bed is not available for exchange.")
            if (
                self.student_id
                and self.student.residential_eligibility
                != target_hostel.resident_category
            ):
                raise ValidationError("Preferred bed does not match residential eligibility.")
            if (
                self.current_occupancy_id
                and self.preferred_bed_id == self.current_occupancy.bed_id
            ):
                raise ValidationError("Preferred bed must differ from the current bed.")
        if self.preferred_hostel_id and self.preferred_hostel.institution_id != self.institution_id:
            raise ValidationError("Preferred hostel must belong to the request institution.")
        if self.preferred_block_id:
            if self.preferred_block.hostel.institution_id != self.institution_id:
                raise ValidationError("Preferred block must belong to the request institution.")
            if self.preferred_hostel_id and self.preferred_block.hostel_id != self.preferred_hostel_id:
                raise ValidationError("Preferred block must belong to the preferred hostel.")
        if self.preferred_floor_id:
            if self.preferred_floor.block.hostel.institution_id != self.institution_id:
                raise ValidationError("Preferred floor must belong to the request institution.")
            if self.preferred_block_id and self.preferred_floor.block_id != self.preferred_block_id:
                raise ValidationError("Preferred floor must belong to the preferred block.")
        if self.expires_at and self.created_at and self.expires_at < self.created_at:
            raise ValidationError("Exchange request expiry cannot precede creation.")


class ExchangeProposal(TimeStampedModel):
    """A concrete direct swap or cycle generated from exchange requests."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_ACCEPTANCE = "pending_acceptance", "Pending Participant Acceptance"
        ACCEPTED = "accepted", "Accepted by All Participants"
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        COMPLETED = "completed", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="exchange_proposals",
    )
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    cycle_length = models.PositiveSmallIntegerField()
    cycle_signature = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        unique=False,
        db_index=True,
        help_text="Canonical request ID set used to prevent duplicate active cycles.",
    )
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_exchange_proposals",
    )

    class Meta:
        db_table = "exchange_proposals"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["expires_at", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["cycle_signature"],
                condition=(
                    Q(cycle_signature__isnull=False)
                    & Q(
                        status__in=[
                            "draft",
                            "pending_acceptance",
                            "accepted",
                            "pending_approval",
                            "approved",
                        ]
                    )
                ),
                name="unique_active_exchange_cycle_signature",
            ),
            models.CheckConstraint(
                condition=Q(cycle_length__gte=2) & Q(cycle_length__lte=5),
                name="exchange_proposal_cycle_length_2_to_5",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gte=models.F("created_at")),
                name="exchange_proposal_expiry_after_creation",
            ),
        ]

    def __str__(self):
        return f"Exchange proposal {self.pk} ({self.status})"

    def clean(self):
        super().clean()
        if self.created_by_id and self.created_by.institution_id not in (None, self.institution_id):
            raise ValidationError("Proposal creator must belong to the proposal institution.")
        if self.expires_at and self.created_at and self.expires_at < self.created_at:
            raise ValidationError("Proposal expiry cannot precede creation.")

    @property
    def hostel_ids(self) -> set:
        """Set of hostel IDs involved in this proposal."""
        if hasattr(self, "_prefetched_objects_cache") and "participants" in self._prefetched_objects_cache:
            return {
                p.current_occupancy.bed.room.floor.block.hostel_id
                for p in self.participants.all()
                if p.current_occupancy_id and p.current_occupancy.bed_id
            }
        return set(
            self.participants.values_list(
                "current_occupancy__bed__room__floor__block__hostel_id",
                flat=True,
            )
        )

    @property
    def is_cross_hostel(self) -> bool:
        """Return whether proposal spans multiple hostels."""
        return len(self.hostel_ids) > 1

    @property
    def classification(self) -> str:
        """Classification: 'cross_hostel' or 'same_hostel'."""
        return "cross_hostel" if self.is_cross_hostel else "same_hostel"

    @property
    def scope_hostel(self):
        """For same-hostel proposals, returns the single Hostel object; otherwise None."""
        if not self.is_cross_hostel:
            if hasattr(self, "_prefetched_objects_cache") and "participants" in self._prefetched_objects_cache:
                for p in self.participants.all():
                    if p.current_occupancy_id and p.current_occupancy.bed_id:
                        return p.current_occupancy.bed.room.floor.block.hostel
            part = self.participants.select_related(
                "current_occupancy__bed__room__floor__block__hostel"
            ).first()
            if part and part.current_occupancy and part.current_occupancy.bed:
                return part.current_occupancy.bed.room.floor.block.hostel
        return None

    @property
    def current_approval(self):
        """Return the latest Approval record for this proposal if any."""
        if hasattr(self, "_prefetched_objects_cache") and "approvals" in self._prefetched_objects_cache:
            approvals = list(self.approvals.all())
            return approvals[0] if approvals else None
        return self.approvals.order_by("-decided_at", "-created_at").first()


class ProposalParticipant(TimeStampedModel):
    """One student's position, occupancy reservation, and destination in a proposal."""

    class ResponseStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        ExchangeProposal,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    exchange_request = models.ForeignKey(
        ExchangeRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="proposal_participations",
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="exchange_proposal_participations",
    )
    current_occupancy = models.ForeignKey(
        StudentOccupancy,
        on_delete=models.PROTECT,
        related_name="exchange_proposal_participations",
    )
    proposed_bed = models.ForeignKey(
        Bed,
        on_delete=models.PROTECT,
        related_name="incoming_exchange_participants",
    )
    sequence_order = models.PositiveSmallIntegerField()
    response_status = models.CharField(
        max_length=20,
        choices=ResponseStatus.choices,
        default=ResponseStatus.PENDING,
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    is_active_reservation = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "exchange_proposal_participants"
        ordering = ["proposal", "sequence_order"]
        indexes = [
            models.Index(fields=["student", "is_active_reservation"]),
            models.Index(fields=["current_occupancy", "is_active_reservation"]),
            models.Index(fields=["exchange_request", "is_active_reservation"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "student"],
                name="unique_student_per_exchange_proposal",
            ),
            models.UniqueConstraint(
                fields=["proposal", "exchange_request"],
                condition=Q(exchange_request__isnull=False),
                name="unique_request_per_exchange_proposal",
            ),
            models.UniqueConstraint(
                fields=["proposal", "sequence_order"],
                name="unique_sequence_per_exchange_proposal",
            ),
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(is_active_reservation=True),
                name="unique_active_exchange_reservation_per_student",
            ),
            models.UniqueConstraint(
                fields=["current_occupancy"],
                condition=Q(is_active_reservation=True),
                name="unique_active_exchange_reservation_per_occupancy",
            ),
            models.UniqueConstraint(
                fields=["exchange_request"],
                condition=Q(is_active_reservation=True) & Q(exchange_request__isnull=False),
                name="unique_active_exchange_reservation_per_request",
            ),
            models.CheckConstraint(
                condition=Q(sequence_order__gte=0) & Q(sequence_order__lte=4),
                name="exchange_participant_sequence_0_to_4",
            ),
        ]

    def __str__(self):
        return f"{self.student.full_name} in proposal {self.proposal_id}"

    def clean(self):
        super().clean()
        if self.exchange_request_id:
            if self.proposal_id and self.proposal.institution_id != self.exchange_request.institution_id:
                raise ValidationError("Proposal and exchange request must share an institution.")
            if self.student_id and self.exchange_request.student_id != self.student_id:
                raise ValidationError("Proposal participant and exchange request student must match.")
        if self.current_occupancy_id and self.current_occupancy.student_id != self.student_id:
            raise ValidationError("Current occupancy must belong to the proposal participant.")
        if self.proposed_bed_id:
            proposed_institution_id = self.proposed_bed.room.floor.block.hostel.institution_id
            if proposed_institution_id != self.proposal.institution_id:
                raise ValidationError("Proposed bed must belong to the proposal institution.")
        if self.proposal_id and self.sequence_order >= self.proposal.cycle_length:
            raise ValidationError("Participant sequence must be within the proposal cycle length.")


class Approval(TimeStampedModel):
    """An authority decision recorded against an exchange proposal or vacant-bed transfer request."""

    class AuthorityRole(models.TextChoices):
        WARDEN = "warden", "Warden"
        CHIEF_WARDEN = "chief_warden", "Chief Warden"

    class Decision(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(
        ExchangeProposal,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="approvals",
    )
    exchange_request = models.ForeignKey(
        ExchangeRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="approvals",
    )
    authority_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="exchange_approvals",
    )
    authority_role = models.CharField(max_length=20, choices=AuthorityRole.choices)
    scope_hostel = models.ForeignKey(
        Hostel,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="exchange_approvals",
    )
    decision = models.CharField(
        max_length=20,
        choices=Decision.choices,
        default=Decision.PENDING,
    )
    remarks = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "exchange_approvals"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["proposal", "decision"]),
            models.Index(fields=["exchange_request", "decision"]),
            models.Index(fields=["authority_user", "decision"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "authority_user", "authority_role", "scope_hostel"],
                condition=Q(proposal__isnull=False),
                name="unique_exchange_approval_authority_scope",
            ),
            models.UniqueConstraint(
                fields=["exchange_request", "authority_user", "authority_role"],
                condition=Q(exchange_request__isnull=False),
                name="unique_vacant_transfer_approval_authority_scope",
            ),
            models.CheckConstraint(
                condition=(
                    Q(proposal__isnull=False) | Q(exchange_request__isnull=False)
                ),
                name="exchange_approval_target_required",
            ),
            models.CheckConstraint(
                condition=(
                    Q(decision="pending", decided_at__isnull=True)
                    | Q(decision__in=["approved", "rejected"], decided_at__isnull=False)
                ),
                name="exchange_approval_decision_timestamp_consistent",
            ),
        ]

    def __str__(self):
        if self.proposal_id:
            return f"{self.authority_role} approval for proposal {self.proposal_id}"
        return f"{self.authority_role} approval for request {self.exchange_request_id}"

    def clean(self):
        super().clean()
        target_institution_id = None
        if self.proposal_id:
            target_institution_id = self.proposal.institution_id
        elif self.exchange_request_id:
            target_institution_id = self.exchange_request.institution_id
        else:
            raise ValidationError("Approval must reference either a proposal or an exchange request.")

        if self.authority_user_id and self.authority_user.institution_id not in (None, target_institution_id):
            raise ValidationError("Approver must belong to the proposal/request institution.")
        if self.scope_hostel_id and self.scope_hostel.institution_id != target_institution_id:
            raise ValidationError("Approval scope hostel must belong to the proposal/request institution.")


class TransferRecord(TimeStampedModel):
    """Immutable historical record of one participant's completed exchange or vacant-bed transfer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_records",
    )
    exchange_request = models.ForeignKey(
        ExchangeRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_records",
    )
    proposal = models.ForeignKey(
        ExchangeProposal,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_records",
    )
    participant = models.OneToOneField(
        ProposalParticipant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_record",
    )
    approval = models.ForeignKey(
        Approval,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfer_records",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="executed_transfers",
    )
    remarks = models.TextField(blank=True)
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="exchange_transfer_records",
    )
    previous_occupancy = models.ForeignKey(
        StudentOccupancy,
        on_delete=models.PROTECT,
        related_name="exchange_transfer_records",
    )
    previous_room = models.ForeignKey(
        Room,
        on_delete=models.PROTECT,
        related_name="outgoing_exchange_transfers",
    )
    previous_bed = models.ForeignKey(
        Bed,
        on_delete=models.PROTECT,
        related_name="outgoing_exchange_transfers",
    )
    new_occupancy = models.ForeignKey(
        StudentOccupancy,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="incoming_transfer_records",
    )
    new_room = models.ForeignKey(
        Room,
        on_delete=models.PROTECT,
        related_name="incoming_exchange_transfers",
    )
    new_bed = models.ForeignKey(
        Bed,
        on_delete=models.PROTECT,
        related_name="incoming_exchange_transfers",
    )
    source_hostel_name = models.CharField(max_length=150, blank=True)
    source_room_number = models.CharField(max_length=50, blank=True)
    source_bed_label = models.CharField(max_length=50, blank=True)
    destination_hostel_name = models.CharField(max_length=150, blank=True)
    destination_room_number = models.CharField(max_length=50, blank=True)
    destination_bed_label = models.CharField(max_length=50, blank=True)
    transferred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "exchange_transfer_records"
        ordering = ["-transferred_at"]
        indexes = [
            models.Index(fields=["proposal", "student"]),
            models.Index(fields=["exchange_request", "student"]),
            models.Index(fields=["student", "transferred_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "student"],
                condition=Q(proposal__isnull=False),
                name="unique_transfer_record_per_proposal_student",
            ),
            models.UniqueConstraint(
                fields=["exchange_request"],
                condition=Q(exchange_request__isnull=False),
                name="unique_transfer_record_per_exchange_request",
            ),
        ]

    def __str__(self):
        if self.proposal_id:
            return f"{self.student.full_name} transfer for proposal {self.proposal_id}"
        return f"{self.student.full_name} vacant-bed transfer for request {self.exchange_request_id}"

    def clean(self):
        super().clean()
        if self.participant_id:
            if self.proposal_id and self.participant.proposal_id != self.proposal_id:
                raise ValidationError("Transfer record proposal must match participant proposal.")
            if self.participant.student_id != self.student_id:
                raise ValidationError("Transfer record student must match participant student.")
            if self.participant.current_occupancy_id != self.previous_occupancy_id:
                raise ValidationError("Transfer record must reference the participant's previous occupancy.")
            if self.participant.current_occupancy.bed_id != self.previous_bed_id:
                raise ValidationError("Previous bed must match the previous occupancy.")
            if self.participant.proposed_bed_id != self.new_bed_id:
                raise ValidationError("New bed must match the participant's proposed bed.")
        if self.exchange_request_id:
            if self.exchange_request.student_id != self.student_id:
                raise ValidationError("Transfer record student must match exchange request student.")
        if self.previous_room_id and self.previous_bed.room_id != self.previous_room_id:
            raise ValidationError("Previous bed must belong to previous room.")
        if self.new_room_id and self.new_bed.room_id != self.new_room_id:
            raise ValidationError("New bed must belong to new room.")


class PolicyRule(TimeStampedModel):
    """Institution-scoped configurable exchange policy value."""

    class Key(models.TextChoices):
        COOLING_OFF_DAYS = "cooling_off_days", "Cooling-off period in days"
        REQUEST_VALIDITY_DAYS = "request_validity_days", "Request validity in days"
        MAX_CYCLE_LENGTH = "max_cycle_length", "Maximum cycle length"
        REQUIRE_FEE_CLEARANCE = "require_fee_clearance", "Require fee clearance"
        REQUIRE_NO_DISCIPLINARY_HOLD = "require_no_disciplinary_hold", "Require no disciplinary hold"
        PROPOSAL_ACCEPTANCE_WINDOW_HOURS = "proposal_acceptance_window_hours", "Proposal acceptance window in hours"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="exchange_policy_rules",
    )
    key = models.CharField(max_length=50, choices=Key.choices)
    value = models.JSONField(default=dict)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "exchange_policy_rules"
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["institution", "key"],
                name="unique_exchange_policy_key_per_institution",
            ),
        ]
        indexes = [
            models.Index(fields=["institution", "is_active"]),
        ]

    def __str__(self):
        return f"{self.institution.code}: {self.key}"


class Notification(TimeStampedModel):
    """Persistent notification associated with an exchange request or proposal."""

    class NotificationType(models.TextChoices):
        PROPOSAL_CREATED = "proposal_created", "Proposal Created"
        ACCEPTANCE_REQUIRED = "acceptance_required", "Acceptance Required"
        ACCEPTANCE_UPDATED = "acceptance_updated", "Acceptance Updated"
        APPROVAL_UPDATED = "approval_updated", "Approval Updated"
        REQUEST_EXPIRED = "request_expired", "Request Expired"
        PROPOSAL_EXPIRED = "proposal_expired", "Proposal Expired"
        TRANSFER_COMPLETED = "transfer_completed", "Transfer Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="exchange_notifications",
    )
    notification_type = models.CharField(max_length=30, choices=NotificationType.choices)
    message = models.TextField()
    exchange_request = models.ForeignKey(
        ExchangeRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    proposal = models.ForeignKey(
        ExchangeProposal,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "exchange_notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "read_at"]),
            models.Index(fields=["recipient", "created_at"]),
        ]

    def __str__(self):
        return f"{self.notification_type} for {self.recipient.email}"


class MatchingJob(TimeStampedModel):
    """Tracks institution-level background matching runs and worker execution lifecycle."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="matching_jobs",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    trigger_reason = models.CharField(max_length=100, blank=True)
    trigger_request = models.ForeignKey(
        ExchangeRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_matching_jobs",
    )
    needs_rerun = models.BooleanField(
        default=False,
        help_text="Flag indicating new events arrived while running, requiring another pass.",
    )
    candidates_found_count = models.IntegerField(default=0)
    proposals_activated_count = models.IntegerField(default=0)
    limit_reached = models.BooleanField(default=False)
    attempts = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "exchange_matching_jobs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["institution", "status"]),
            models.Index(fields=["institution", "created_at"]),
        ]

    def __str__(self):
        return f"MatchingJob {self.id} ({self.institution.code}) - {self.status}"


class MatchingOutbox(TimeStampedModel):
    """
    Durable transactional outbox for broker failure resilience.
    Guarantees that matching opportunities are not lost if the Celery message broker is down.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="matching_outbox_events",
    )
    event_type = models.CharField(max_length=50)
    request_id = models.UUIDField(null=True, blank=True)
    proposal_id = models.UUIDField(null=True, blank=True)
    processed = models.BooleanField(default=False, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        db_table = "exchange_matching_outbox"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["institution", "processed", "created_at"]),
        ]

    def __str__(self):
        return f"Outbox {self.event_type} ({self.institution.code}) - {'Done' if self.processed else 'Pending'}"


class SuppressedCycle(TimeStampedModel):
    """
    Suppresses repeat proposals for declined or expired cycles.
    Suppressed based on the exact directed cycle signature and participant request revisions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.CASCADE,
        related_name="suppressed_cycles",
    )
    cycle_signature = models.CharField(max_length=1024, db_index=True)
    proposal = models.ForeignKey(
        ExchangeProposal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suppressions",
    )
    reason = models.CharField(max_length=30)  # "declined", "expired", "rejected"
    request_revisions = models.JSONField(
        default=dict,
        help_text="Map of request_id to timestamp string at time of decline/expiry.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "exchange_suppressed_cycles"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["institution", "cycle_signature", "is_active"]),
        ]

    def __str__(self):
        return f"Suppression for {self.cycle_signature[:30]} ({self.reason})"
