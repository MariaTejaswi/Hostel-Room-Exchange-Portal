"""Read-only, institution-scoped selectors for P04 M7 reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import (
    Avg,
    Count,
    DateTimeField,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    QuerySet,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.core.models import AuditLog
from apps.hostels.models import Hostel
from apps.hostels.selectors import get_warden_assigned_hostel_ids

from .models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    ProposalParticipant,
    TransferRecord,
)

EXCHANGE_AUDIT_FILTER = Q(action__startswith="EXCHANGE_") | Q(
    action__startswith="VACANT_TRANSFER_"
)


@dataclass(frozen=True)
class ExchangeReportScope:
    institution_id: int
    role: str
    label: str
    hostel_ids: tuple[int, ...] | None

    @property
    def institution_wide(self) -> bool:
        return self.hostel_ids is None


@dataclass(frozen=True)
class ReportWindow:
    start_date: date
    end_date: date
    start_at: datetime
    end_at: datetime


def get_exchange_report_scope(user) -> ExchangeReportScope:
    """Resolve the explicit read-only report scope for an authenticated user."""
    if not user or not user.is_authenticated or not user.institution_id:
        raise PermissionDenied(
            "Exchange analytics require an institution-scoped account."
        )

    cached_scope = getattr(user, "_exchange_report_scope_cache", None)
    if cached_scope and cached_scope.institution_id == user.institution_id:
        return cached_scope

    role = user.primary_role
    if role == "Dean of Student Welfare":
        scope = ExchangeReportScope(
            institution_id=user.institution_id,
            role="dean",
            label="Dean institution-wide read-only analytics",
            hostel_ids=None,
        )
    elif role == "Chief Warden":
        scope = ExchangeReportScope(
            institution_id=user.institution_id,
            role="chief_warden",
            label="Chief Warden institution-wide read-only analytics",
            hostel_ids=None,
        )
    elif role == "Warden":
        hostel_ids = tuple(
            sorted(
                get_warden_assigned_hostel_ids(user),
                key=str,
            )
        )
        scope = ExchangeReportScope(
            institution_id=user.institution_id,
            role="warden",
            label="Assigned-hostel read-only analytics",
            hostel_ids=hostel_ids,
        )
    else:
        raise PermissionDenied("Your role does not have access to exchange analytics.")
    user._exchange_report_scope_cache = scope
    return scope


def get_exchange_report_hostels(user) -> QuerySet:
    """Return only hostels the user may select in the report filter."""
    scope = get_exchange_report_scope(user)
    queryset = Hostel.objects.filter(institution_id=scope.institution_id).order_by(
        "name"
    )
    if scope.hostel_ids is not None:
        queryset = queryset.filter(id__in=scope.hostel_ids)
    return queryset


def make_report_window(start_date: date, end_date: date) -> ReportWindow:
    """Convert inclusive local dates to a timezone-aware half-open interval."""
    if end_date < start_date:
        raise ValueError("End date cannot precede start date.")
    current_tz = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(start_date, time.min), current_tz)
    end_at = timezone.make_aware(
        datetime.combine(end_date + timedelta(days=1), time.min),
        current_tz,
    )
    return ReportWindow(
        start_date=start_date,
        end_date=end_date,
        start_at=start_at,
        end_at=end_at,
    )


def _effective_hostel_ids(
    scope: ExchangeReportScope,
    hostel_id: int | None,
) -> tuple[int, ...] | None:
    if hostel_id is None:
        return scope.hostel_ids

    selected_hostel_exists = Hostel.objects.filter(
        id=hostel_id,
        institution_id=scope.institution_id,
    ).exists()
    if not selected_hostel_exists:
        raise PermissionDenied("The selected hostel is outside your institution.")
    if scope.hostel_ids is not None and hostel_id not in scope.hostel_ids:
        raise PermissionDenied(
            "The selected hostel is outside your assigned reporting scope."
        )
    return (hostel_id,)


def _scope_requests(queryset: QuerySet, hostel_ids: tuple[int, ...] | None) -> QuerySet:
    if hostel_ids is None:
        return queryset
    return queryset.filter(
        current_occupancy__bed__room__floor__block__hostel_id__in=hostel_ids
    )


def _scope_proposals(
    queryset: QuerySet, hostel_ids: tuple[int, ...] | None
) -> QuerySet:
    if hostel_ids is None:
        return queryset
    return queryset.filter(
        participants__current_occupancy__bed__room__floor__block__hostel_id__in=hostel_ids
    ).distinct()


def _scope_approvals(
    queryset: QuerySet, hostel_ids: tuple[int, ...] | None
) -> QuerySet:
    if hostel_ids is None:
        return queryset
    return queryset.filter(
        Q(
            proposal__participants__current_occupancy__bed__room__floor__block__hostel_id__in=hostel_ids
        )
        | Q(
            exchange_request__current_occupancy__bed__room__floor__block__hostel_id__in=hostel_ids
        )
    )


def _scope_transfers(
    queryset: QuerySet, hostel_ids: tuple[int, ...] | None
) -> QuerySet:
    if hostel_ids is None:
        return queryset
    return queryset.filter(previous_bed__room__floor__block__hostel_id__in=hostel_ids)


def _safe_uuid_values(values) -> set[UUID]:
    result: set[UUID] = set()
    for value in values:
        try:
            result.add(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def _format_duration(value: timedelta | None) -> str | None:
    if value is None:
        return None
    total_minutes = max(0, round(value.total_seconds() / 60))
    if total_minutes < 1:
        return "Less than 1 minute"
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts[:2])


def build_exchange_report(
    *,
    user,
    window: ReportWindow,
    hostel_id: int | None = None,
) -> dict:
    """Build all M7 aggregate metrics without mutating workflow data."""
    scope = get_exchange_report_scope(user)
    hostel_ids = _effective_hostel_ids(scope, hostel_id)

    request_queryset = _scope_requests(
        ExchangeRequest.objects.filter(
            institution_id=scope.institution_id,
            submitted_at__gte=window.start_at,
            submitted_at__lt=window.end_at,
        ),
        hostel_ids,
    )
    request_summary = request_queryset.aggregate(
        total=Count("id"),
        exchange=Count(
            "id",
            filter=Q(request_type=ExchangeRequest.RequestType.EXCHANGE),
        ),
        vacant_transfer=Count(
            "id",
            filter=Q(request_type=ExchangeRequest.RequestType.VACANT_TRANSFER),
        ),
        broad_preferences=Count("id", filter=Q(preferred_bed__isnull=True)),
        explicit_targets=Count("id", filter=Q(preferred_bed__isnull=False)),
    )

    status_labels = dict(ExchangeRequest.Status.choices)
    status_distribution = list(
        request_queryset.values("status")
        .annotate(request_count=Count("id"))
        .order_by("status")
    )
    for row in status_distribution:
        row["status_label"] = status_labels.get(row["status"], row["status"])

    volume_by_hostel = list(
        request_queryset.values(
            "current_occupancy__bed__room__floor__block__hostel_id",
            "current_occupancy__bed__room__floor__block__hostel__name",
        )
        .annotate(
            request_count=Count("id"),
            exchange_count=Count(
                "id",
                filter=Q(request_type=ExchangeRequest.RequestType.EXCHANGE),
            ),
            vacant_transfer_count=Count(
                "id",
                filter=Q(request_type=ExchangeRequest.RequestType.VACANT_TRANSFER),
            ),
        )
        .order_by("current_occupancy__bed__room__floor__block__hostel__name")
    )
    for row in volume_by_hostel:
        row["hostel_id"] = row.pop(
            "current_occupancy__bed__room__floor__block__hostel_id"
        )
        row["hostel_name"] = (
            row.pop("current_occupancy__bed__room__floor__block__hostel__name")
            or "Not available"
        )

    volume_by_day = list(
        request_queryset.annotate(
            report_day=TruncDate(
                "submitted_at",
                tzinfo=timezone.get_current_timezone(),
            )
        )
        .values("report_day")
        .annotate(request_count=Count("id"))
        .order_by("report_day")
    )
    max_daily_volume = max(
        (row["request_count"] for row in volume_by_day),
        default=0,
    )
    for row in volume_by_day:
        row["bar_percent"] = (
            round((row["request_count"] / max_daily_volume) * 100)
            if max_daily_volume
            else 0
        )

    activated_participation = ProposalParticipant.objects.filter(
        exchange_request_id=OuterRef("pk")
    ).exclude(proposal__status=ExchangeProposal.Status.DRAFT)
    matched_requests = (
        request_queryset.filter(request_type=ExchangeRequest.RequestType.EXCHANGE)
        .annotate(was_matched=Exists(activated_participation))
        .filter(was_matched=True)
        .count()
    )
    match_denominator = request_summary["exchange"]
    match_rate = (
        (matched_requests / match_denominator) * 100 if match_denominator else None
    )

    explicit_room_targets = list(
        request_queryset.filter(preferred_bed__isnull=False)
        .values(
            "preferred_bed__room_id",
            "preferred_bed__room__room_number",
            "preferred_bed__room__floor__block__hostel__name",
        )
        .annotate(request_count=Count("id", distinct=True))
        .order_by(
            "-request_count",
            "preferred_bed__room__floor__block__hostel__name",
            "preferred_bed__room__room_number",
        )[:10]
    )
    for row in explicit_room_targets:
        row["room_id"] = row.pop("preferred_bed__room_id")
        row["room_number"] = row.pop("preferred_bed__room__room_number")
        row["hostel_name"] = (
            row.pop("preferred_bed__room__floor__block__hostel__name")
            or "Not available"
        )

    proposal_queryset = _scope_proposals(
        ExchangeProposal.objects.filter(institution_id=scope.institution_id),
        hostel_ids,
    )
    draft_candidates = proposal_queryset.filter(
        status=ExchangeProposal.Status.DRAFT,
        created_at__gte=window.start_at,
        created_at__lt=window.end_at,
    ).count()

    activation_event_targets = list(
        AuditLog.objects.filter(
            institution_id=scope.institution_id,
            action="EXCHANGE_PROPOSAL_ACTIVATED",
            target_type="ExchangeProposal",
            timestamp__gte=window.start_at,
            timestamp__lt=window.end_at,
        )
        .values_list("target_id", flat=True)
        .distinct()
    )
    activation_event_ids = _safe_uuid_values(activation_event_targets)
    activated_from_events = proposal_queryset.filter(
        id__in=activation_event_ids
    ).count()
    activated_from_timestamp = proposal_queryset.exclude(
        status=ExchangeProposal.Status.DRAFT
    ).filter(
        matched_at__gte=window.start_at,
        matched_at__lt=window.end_at,
    )
    if activation_event_ids:
        activated_from_timestamp = activated_from_timestamp.exclude(
            id__in=activation_event_ids
        )
    activated_fallback_count = activated_from_timestamp.count()
    missing_activation_history = proposal_queryset.exclude(
        status=ExchangeProposal.Status.DRAFT
    ).filter(
        matched_at__isnull=True,
        created_at__gte=window.start_at,
        created_at__lt=window.end_at,
    )
    if activation_event_ids:
        missing_activation_history = missing_activation_history.exclude(
            id__in=activation_event_ids
        )
    missing_activation_history_count = missing_activation_history.count()

    approval_queryset = _scope_approvals(
        Approval.objects.filter(
            Q(proposal__institution_id=scope.institution_id)
            | Q(exchange_request__institution_id=scope.institution_id),
            decision__in=[Approval.Decision.APPROVED, Approval.Decision.REJECTED],
            decided_at__gte=window.start_at,
            decided_at__lt=window.end_at,
        ),
        hostel_ids,
    )
    authority_patterns = list(
        approval_queryset.values(
            "authority_user_id",
            "authority_user__full_name",
            "authority_role",
        )
        .annotate(
            approved_count=Count(
                "id",
                filter=Q(decision=Approval.Decision.APPROVED),
                distinct=True,
            ),
            rejected_count=Count(
                "id",
                filter=Q(decision=Approval.Decision.REJECTED),
                distinct=True,
            ),
            decision_count=Count("id", distinct=True),
        )
        .order_by("authority_role", "authority_user__full_name")
    )
    authority_role_labels = dict(Approval.AuthorityRole.choices)
    for row in authority_patterns:
        row["authority_name"] = row.pop("authority_user__full_name")
        row["authority_role_label"] = authority_role_labels.get(
            row["authority_role"],
            row["authority_role"],
        )
        row["approval_rate"] = (
            (row["approved_count"] / row["decision_count"]) * 100
            if row["decision_count"]
            else None
        )

    transfer_queryset = _scope_transfers(
        TransferRecord.objects.filter(
            institution_id=scope.institution_id,
            transferred_at__gte=window.start_at,
            transferred_at__lt=window.end_at,
        ),
        hostel_ids,
    )
    completion_summary = transfer_queryset.aggregate(
        completed_exchanges=Count(
            "proposal_id",
            filter=Q(proposal_id__isnull=False),
            distinct=True,
        ),
        direct_exchanges=Count(
            "proposal_id",
            filter=Q(proposal__cycle_length=2),
            distinct=True,
        ),
        multi_party_exchanges=Count(
            "proposal_id",
            filter=Q(proposal__cycle_length__gt=2),
            distinct=True,
        ),
        completed_vacant_transfers=Count(
            "exchange_request_id",
            filter=Q(proposal_id__isnull=True, exchange_request_id__isnull=False),
            distinct=True,
        ),
        student_movements=Count("id"),
    )

    duration_queryset = transfer_queryset.annotate(
        request_submitted_at=Coalesce(
            "exchange_request__submitted_at",
            "participant__exchange_request__submitted_at",
            output_field=DateTimeField(),
        )
    ).filter(request_submitted_at__isnull=False)
    duration_summary = duration_queryset.annotate(
        completion_duration=ExpressionWrapper(
            F("transferred_at") - F("request_submitted_at"),
            output_field=DurationField(),
        )
    ).aggregate(
        average=Avg("completion_duration"),
        sample_count=Count("id"),
    )
    completion_summary["average_completion_time"] = duration_summary["average"]
    completion_summary["average_completion_label"] = _format_duration(
        duration_summary["average"]
    )
    completion_summary["duration_sample_count"] = duration_summary["sample_count"]
    completion_summary["duration_unavailable_count"] = (
        completion_summary["student_movements"] - duration_summary["sample_count"]
    )

    return {
        "scope": scope,
        "window": window,
        "request_summary": request_summary,
        "status_distribution": status_distribution,
        "volume_by_hostel": volume_by_hostel,
        "volume_by_day": volume_by_day,
        "match": {
            "matched_requests": matched_requests,
            "eligible_requests": match_denominator,
            "rate": match_rate,
        },
        "targets": {
            "explicit_rooms": explicit_room_targets,
            "explicit_request_count": request_summary["explicit_targets"],
            "broad_request_count": request_summary["broad_preferences"],
        },
        "workflow": {
            "draft_candidates": draft_candidates,
            "activated_proposals": (activated_from_events + activated_fallback_count),
            "activation_event_count": activated_from_events,
            "activation_fallback_count": activated_fallback_count,
            "missing_activation_history_count": missing_activation_history_count,
        },
        "authority_patterns": authority_patterns,
        "completion": completion_summary,
    }


def get_report_transfer_queryset(
    *,
    user,
    window: ReportWindow,
    hostel_id: int | None = None,
) -> QuerySet:
    """Return paginatable, privacy-minimal completion detail for the report."""
    scope = get_exchange_report_scope(user)
    hostel_ids = _effective_hostel_ids(scope, hostel_id)
    return (
        _scope_transfers(
            TransferRecord.objects.filter(
                institution_id=scope.institution_id,
                transferred_at__gte=window.start_at,
                transferred_at__lt=window.end_at,
            ),
            hostel_ids,
        )
        .select_related(
            "proposal",
            "exchange_request",
            "approval",
        )
        .order_by("-transferred_at", "id")
    )


def get_exchange_audit_queryset(
    *,
    user,
    window: ReportWindow,
    hostel_id: int | None = None,
) -> QuerySet:
    """Return exchange audit history limited to the report's institution/hostels."""
    scope = get_exchange_report_scope(user)
    hostel_ids = _effective_hostel_ids(scope, hostel_id)
    queryset = AuditLog.objects.filter(
        EXCHANGE_AUDIT_FILTER,
        institution_id=scope.institution_id,
        timestamp__gte=window.start_at,
        timestamp__lt=window.end_at,
    ).select_related("acting_user")

    if hostel_ids is None:
        return queryset.order_by("-timestamp", "-id")

    request_ids = list(
        _scope_requests(
            ExchangeRequest.objects.filter(institution_id=scope.institution_id),
            hostel_ids,
        ).values_list("id", flat=True)
    )
    proposal_ids = list(
        _scope_proposals(
            ExchangeProposal.objects.filter(institution_id=scope.institution_id),
            hostel_ids,
        ).values_list("id", flat=True)
    )
    return queryset.filter(
        Q(
            target_type="ExchangeRequest",
            target_id__in=[str(value) for value in request_ids],
        )
        | Q(
            target_type="ExchangeProposal",
            target_id__in=[str(value) for value in proposal_ids],
        )
    ).order_by("-timestamp", "-id")
