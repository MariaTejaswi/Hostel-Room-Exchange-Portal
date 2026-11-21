from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.hostels.models import Hostel, OperationalStatus, Room, StudentOccupancy

from .eligibility import (
    ExternalEligibilityAdapter,
    validate_exchange_request_eligibility,
)
from .models import (
    DEFAULT_MAX_EXCHANGE_CYCLE_LENGTH,
    MAX_EXCHANGE_CYCLE_LENGTH,
    MIN_EXCHANGE_CYCLE_LENGTH,
    ExchangeProposal,
    ExchangeRequest,
    PolicyRule,
    ProposalParticipant,
)

ACTIVE_PROPOSAL_STATUSES = (
    ExchangeProposal.Status.DRAFT,
    ExchangeProposal.Status.PENDING_ACCEPTANCE,
    ExchangeProposal.Status.ACCEPTED,
    ExchangeProposal.Status.PENDING_APPROVAL,
    ExchangeProposal.Status.APPROVED,
)
ROOM_TYPE_LABELS = dict(Room.RoomType.choices)


def get_institution_max_cycle_length(institution) -> int:
    """Return the institution's validated cycle cap, defaulting to five."""
    if institution is None:
        return DEFAULT_MAX_EXCHANGE_CYCLE_LENGTH
    rule = PolicyRule.objects.filter(
        institution=institution,
        key=PolicyRule.Key.MAX_CYCLE_LENGTH,
        is_active=True,
    ).first()
    if rule is None:
        return DEFAULT_MAX_EXCHANGE_CYCLE_LENGTH
    value = rule.value.get("value") if isinstance(rule.value, dict) else rule.value
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Configured maximum chain length must be a whole number between 2 and 5.") from exc
    if not MIN_EXCHANGE_CYCLE_LENGTH <= value <= MAX_EXCHANGE_CYCLE_LENGTH:
        raise ValidationError("Configured maximum chain length must be between 2 and 5.")
    return value


def get_effective_max_cycle_length(institution, requested_max_length: int | None = None) -> int:
    """Apply the institutional cap while permitting a caller to request a stricter bound."""
    configured = get_institution_max_cycle_length(institution)
    if requested_max_length is None:
        return configured
    if not MIN_EXCHANGE_CYCLE_LENGTH <= requested_max_length <= MAX_EXCHANGE_CYCLE_LENGTH:
        raise ValidationError("Requested maximum chain length must be between 2 and 5.")
    return min(configured, requested_max_length)


@dataclass(frozen=True, slots=True)
class _BedSnapshot:
    bed_id: int
    bed_label: str
    room_number: str
    room_type: str
    occupancy_category: str
    occupancy_category_normalised: str
    accessibility_normalised: str
    floor_id: int
    floor_label: str
    block_id: int
    block_name: str
    hostel_id: int
    hostel_name: str
    hostel_institution_id: int
    hostel_resident_category: str
    is_matchable: bool


@dataclass(frozen=True, slots=True)
class _RequestSnapshot:
    request_id: str
    institution_id: int
    student_id: int
    student_name: str
    student_registration: str
    student_residential_eligibility: str
    current_occupancy_id: int
    current_bed: _BedSnapshot
    preferred_hostel_id: int | None
    preferred_bed_id: int | None
    preferred_block_id: int | None
    preferred_floor_id: int | None
    preferred_room_type: str
    preferred_occupancy_category: str
    preferred_occupancy_category_normalised: str
    preferred_accessibility_attributes: str
    preferred_accessibility_tokens: tuple[str, ...]


@dataclass(frozen=True)
class ExchangeGraph:
    """Directed request graph; an edge A -> B means A can receive B's room."""

    vertices: dict[str, ExchangeRequest]
    edges: dict[str, tuple[str, ...]]
    snapshots: dict[str, _RequestSnapshot] | None = None


@dataclass(frozen=True)
class ExchangeCycle:
    """A directed cycle in participant order."""

    request_ids: tuple[str, ...]

    @property
    def signature(self) -> str:
        """
        Canonical directed cycle signature.
        Rotates the cycle so the lexicographically smallest request_id is first.
        Preserves rotation direction so that opposing directed cycles remain distinct.
        """
        if not self.request_ids:
            return ""
        min_elem = min(self.request_ids)
        idx = self.request_ids.index(min_elem)
        canonical = self.request_ids[idx:] + self.request_ids[:idx]
        return "->".join(str(rid) for rid in canonical)


@dataclass(frozen=True)
class CandidateParticipant:
    """A single student's current allocation, proposed allocation, and preference explanation."""

    request_id: str
    student_id: int
    student_name: str
    student_registration: str
    current_occupancy_id: int
    current_bed_id: int
    current_bed_label: str
    current_room_number: str
    current_hostel_id: int
    current_hostel_name: str
    proposed_bed_id: int
    proposed_bed_label: str
    proposed_room_number: str
    proposed_hostel_id: int
    proposed_hostel_name: str
    preference_match_explanation: str


@dataclass(frozen=True)
class CandidateMatch:
    """A discovered candidate exchange cycle (direct swap or 3-5 person rotation)."""

    cycle_length: int
    is_cross_hostel: bool
    classification: str  # "same_hostel" or "cross_hostel"
    participants: tuple[CandidateParticipant, ...]
    request_ids: tuple[str, ...]
    cycle_signature: str
    detected_at: datetime

    @property
    def summary(self) -> str:
        return f"{self.cycle_length}-way {self.classification} match ({self.cycle_signature})"


@dataclass(frozen=True)
class MatchingMetrics:
    """Performance and statistical metrics from a matching engine evaluation."""

    graph_nodes: int
    graph_edges: int
    total_candidates: int
    candidates_by_length: dict[int, int]
    same_hostel_count: int
    cross_hostel_count: int
    policy_resolution_time_seconds: float
    data_loading_time_seconds: float
    eligibility_evaluation_time_seconds: float
    graph_building_time_seconds: float
    cycle_detection_time_seconds: float
    candidate_materialization_time_seconds: float
    explanation_generation_time_seconds: float
    persistence_time_seconds: float
    total_time_seconds: float
    total_with_persistence_time_seconds: float
    limit_reached: bool


@dataclass(frozen=True)
class MatchingEngineResult:
    """Consolidated result returned by the matching engine service."""

    institution_id: int
    institution_name: str
    candidates: list[CandidateMatch]
    metrics: MatchingMetrics
    proposals_created: list[ExchangeProposal]


def _normalise_text(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _accessibility_satisfied(requested: str, available: str) -> bool:
    requested_tokens = [
        _normalise_text(token)
        for token in (requested or "").replace(";", ",").split(",")
        if _normalise_text(token)
    ]
    available_text = _normalise_text(available)
    return all(token in available_text for token in requested_tokens)


def _accessibility_tokens(value: str) -> list[str]:
    return [
        _normalise_text(token)
        for token in (value or "").replace(";", ",").split(",")
        if _normalise_text(token)
    ]


def _accessibility_ngrams(value: str, size: int = 3) -> set[str]:
    normalised = _normalise_text(value)
    if len(normalised) < size:
        return {normalised} if normalised else set()
    return {
        normalised[index : index + size]
        for index in range(len(normalised) - size + 1)
    }


def room_satisfies_request(*, request, bed) -> bool:
    """Return whether a candidate bed satisfies every stored request preference."""
    room = bed.room
    floor = room.floor
    block = floor.block
    hostel = block.hostel

    if request.institution_id != hostel.institution_id:
        return False
    if (
        not hostel.status == Hostel.Status.ACTIVE
        or not block.is_active
        or not floor.is_active
        or not room.is_active
        or room.operational_status != OperationalStatus.IN_SERVICE
        or bed.operational_status != OperationalStatus.IN_SERVICE
        or not bed.is_active
    ):
        return False
    if request.student.residential_eligibility != hostel.resident_category:
        return False
    if request.preferred_hostel_id and request.preferred_hostel_id != hostel.id:
        return False
    if request.preferred_bed_id and request.preferred_bed_id != bed.id:
        return False
    if request.preferred_block_id and request.preferred_block_id != block.id:
        return False
    if request.preferred_floor_id and request.preferred_floor_id != floor.id:
        return False
    if request.preferred_room_type and request.preferred_room_type != room.room_type:
        return False
    if (
        request.preferred_occupancy_category
        and _normalise_text(request.preferred_occupancy_category)
        != _normalise_text(room.occupancy_category)
    ):
        return False
    return _accessibility_satisfied(
        request.preferred_accessibility_attributes,
        room.accessibility_attributes,
    )


def explain_preference_match(request: ExchangeRequest, bed) -> str:
    """Return a concise, human-readable explanation of why bed satisfies request preferences."""
    reasons = []
    room = bed.room
    floor = room.floor
    block = floor.block
    hostel = block.hostel

    if request.preferred_bed_id and request.preferred_bed_id == bed.id:
        reasons.append(f"Target bed {bed.bed_label} in Room {room.room_number}")
    if request.preferred_hostel_id and request.preferred_hostel_id == hostel.id:
        reasons.append(f"Hostel matches preference ({hostel.name})")
    if request.preferred_block_id and request.preferred_block_id == block.id:
        reasons.append(f"Block matches preference ({block.name})")
    if request.preferred_floor_id and request.preferred_floor_id == floor.id:
        reasons.append(f"Floor matches preference ({floor.floor_label})")
    if request.preferred_room_type and request.preferred_room_type == room.room_type:
        reasons.append(f"Room type matches preference ({room.get_room_type_display()})")
    if request.preferred_occupancy_category:
        reasons.append(f"Occupancy category matches preference ({room.occupancy_category})")
    if request.preferred_accessibility_attributes:
        reasons.append("Accessibility requirements satisfied")

    if not reasons:
        reasons.append(f"Compatible accommodation in {hostel.name} (Room {room.room_number}, Bed {bed.bed_label})")

    return "; ".join(reasons)


def _bed_snapshot(bed) -> _BedSnapshot:
    room = bed.room
    floor = room.floor
    block = floor.block
    hostel = block.hostel
    return _BedSnapshot(
        bed_id=bed.id,
        bed_label=getattr(bed, "bed_label", "") or "",
        room_number=getattr(room, "room_number", "") or "",
        room_type=room.room_type,
        occupancy_category=room.occupancy_category,
        occupancy_category_normalised=_normalise_text(room.occupancy_category),
        accessibility_normalised=_normalise_text(room.accessibility_attributes),
        floor_id=floor.id,
        floor_label=getattr(floor, "floor_label", "") or "",
        block_id=block.id,
        block_name=getattr(block, "name", "") or "",
        hostel_id=hostel.id,
        hostel_name=getattr(hostel, "name", "") or "",
        hostel_institution_id=hostel.institution_id,
        hostel_resident_category=hostel.resident_category,
        is_matchable=(
            hostel.status == Hostel.Status.ACTIVE
            and block.is_active
            and floor.is_active
            and room.is_active
            and room.operational_status == OperationalStatus.IN_SERVICE
            and bed.is_active
            and bed.operational_status == OperationalStatus.IN_SERVICE
        ),
    )


def _request_snapshot(request: ExchangeRequest) -> _RequestSnapshot:
    return _RequestSnapshot(
        request_id=str(request.id),
        institution_id=request.institution_id,
        student_id=request.student_id,
        student_name=getattr(request.student, "full_name", "") or "",
        student_registration=getattr(request.student, "registration_number", "") or "",
        student_residential_eligibility=request.student.residential_eligibility,
        current_occupancy_id=getattr(request.current_occupancy, "id", 0) or 0,
        current_bed=_bed_snapshot(request.current_occupancy.bed),
        preferred_hostel_id=request.preferred_hostel_id,
        preferred_bed_id=request.preferred_bed_id,
        preferred_block_id=request.preferred_block_id,
        preferred_floor_id=request.preferred_floor_id,
        preferred_room_type=request.preferred_room_type,
        preferred_occupancy_category=request.preferred_occupancy_category,
        preferred_occupancy_category_normalised=_normalise_text(
            request.preferred_occupancy_category
        ),
        preferred_accessibility_attributes=request.preferred_accessibility_attributes,
        preferred_accessibility_tokens=tuple(
            _accessibility_tokens(request.preferred_accessibility_attributes)
        ),
    )


def _snapshot_satisfies_request(
    request: _RequestSnapshot,
    available: _BedSnapshot,
) -> bool:
    """Apply the full room compatibility policy using pre-normalised values."""
    if request.institution_id != available.hostel_institution_id:
        return False
    if not available.is_matchable:
        return False
    if request.student_residential_eligibility != available.hostel_resident_category:
        return False
    if request.preferred_hostel_id and request.preferred_hostel_id != available.hostel_id:
        return False
    if request.preferred_bed_id and request.preferred_bed_id != available.bed_id:
        return False
    if request.preferred_block_id and request.preferred_block_id != available.block_id:
        return False
    if request.preferred_floor_id and request.preferred_floor_id != available.floor_id:
        return False
    if request.preferred_room_type and request.preferred_room_type != available.room_type:
        return False
    if (
        request.preferred_occupancy_category_normalised
        and request.preferred_occupancy_category_normalised
        != available.occupancy_category_normalised
    ):
        return False
    return all(
        token in available.accessibility_normalised
        for token in request.preferred_accessibility_tokens
    )


def _explain_preference_snapshot(
    request: _RequestSnapshot,
    available: _BedSnapshot,
) -> str:
    reasons = []
    if request.preferred_bed_id and request.preferred_bed_id == available.bed_id:
        reasons.append(
            f"Target bed {available.bed_label} in Room {available.room_number}"
        )
    if request.preferred_hostel_id and request.preferred_hostel_id == available.hostel_id:
        reasons.append(f"Hostel matches preference ({available.hostel_name})")
    if request.preferred_block_id and request.preferred_block_id == available.block_id:
        reasons.append(f"Block matches preference ({available.block_name})")
    if request.preferred_floor_id and request.preferred_floor_id == available.floor_id:
        reasons.append(f"Floor matches preference ({available.floor_label})")
    if request.preferred_room_type and request.preferred_room_type == available.room_type:
        room_type_label = ROOM_TYPE_LABELS.get(available.room_type, available.room_type)
        reasons.append(f"Room type matches preference ({room_type_label})")
    if request.preferred_occupancy_category:
        reasons.append(
            f"Occupancy category matches preference ({available.occupancy_category})"
        )
    if request.preferred_accessibility_attributes:
        reasons.append("Accessibility requirements satisfied")
    if not reasons:
        reasons.append(
            "Compatible accommodation in "
            f"{available.hostel_name} (Room {available.room_number}, "
            f"Bed {available.bed_label})"
        )
    return "; ".join(reasons)


def _request_is_graph_eligible(
    request: ExchangeRequest,
    *,
    now: datetime,
    reserved_request_ids: set,
    configured_policies: dict,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> bool:
    """
    Validate that an exchange request is eligible to be a graph node.
    Enforces status, expiry, active occupancy, operational status, gender-block
    alignment, active proposal exclusion, cooling-off, and fee/disciplinary clearance.
    """
    if request.status != ExchangeRequest.Status.OPEN:
        return False
    if request.expires_at is not None and request.expires_at <= now:
        return False

    # Only occupied-bed exchange requests participate in cycle matching
    if getattr(request, "request_type", None) and request.request_type != ExchangeRequest.RequestType.EXCHANGE:
        return False

    # Exclude requests committed to active proposals
    if str(request.id) in reserved_request_ids or request.id in reserved_request_ids:
        return False

    occupancy = request.current_occupancy
    if (
        occupancy is None
        or occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE
        or occupancy.student_id != request.student_id
        or occupancy.institution_id != request.institution_id
    ):
        return False

    bed = occupancy.bed
    if bed is None or not bed.is_active or bed.operational_status != OperationalStatus.IN_SERVICE:
        return False
    room = bed.room
    if room is None or not room.is_active or room.operational_status != OperationalStatus.IN_SERVICE:
        return False
    floor = room.floor
    if floor is None or not floor.is_active:
        return False
    block = floor.block
    if block is None or not block.is_active:
        return False
    hostel = block.hostel
    if hostel is None or hostel.status != Hostel.Status.ACTIVE or hostel.institution_id != request.institution_id:
        return False

    student = request.student
    if student.residential_eligibility not in (
        Hostel.ResidentCategory.MALE,
        Hostel.ResidentCategory.FEMALE,
    ):
        return False
    if hostel.resident_category != student.residential_eligibility:
        return False

    # Cooling-off validation
    cooling_off_val = configured_policies.get(PolicyRule.Key.COOLING_OFF_DAYS)
    if cooling_off_val is not None:
        try:
            cooling_days = int(cooling_off_val)
            if cooling_days > 0 and now.date() < occupancy.start_date + timedelta(days=cooling_days):
                return False
        except (ValueError, TypeError):
            return False

    # Fee clearance validation (evaluated once per candidate student, never per edge)
    req_fee = configured_policies.get(PolicyRule.Key.REQUIRE_FEE_CLEARANCE)
    if req_fee:
        if status_adapter is None:
            return False
        fee_res = status_adapter.check_fee_status(student)
        if (
            not fee_res.available
            or fee_res.status in ("unknown", "unavailable", "blocked")
            or not fee_res.eligible
        ):
            return False

    # Disciplinary hold validation
    req_disc = configured_policies.get(PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD)
    if req_disc:
        if status_adapter is None:
            return False
        disc_res = status_adapter.check_disciplinary_status(student)
        if (
            not disc_res.available
            or disc_res.status in ("unknown", "unavailable", "blocked")
            or not disc_res.eligible
        ):
            return False

    return True


def _request_queryset(*, institution=None, now=None):
    now = now or timezone.now()
    queryset = ExchangeRequest.objects.filter(
        request_type=ExchangeRequest.RequestType.EXCHANGE,
        status=ExchangeRequest.Status.OPEN,
    ).filter(
        models_q_expiry(now)
    )
    if institution is not None:
        queryset = queryset.filter(institution=institution)
    return queryset.select_related(
        "student",
        "institution",
        "current_occupancy__bed__room__floor__block__hostel",
        "preferred_hostel",
        "preferred_bed__room__floor__block__hostel",
        "preferred_block__hostel",
        "preferred_floor__block__hostel",
    )


def models_q_expiry(now):
    from django.db.models import Q

    return Q(expires_at__isnull=True) | Q(expires_at__gt=now)


def build_exchange_graph(
    *,
    institution=None,
    requests: Iterable[ExchangeRequest] | None = None,
    now=None,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> ExchangeGraph:
    """Load eligible open requests and create preference-compatible directed edges without DB queries inside traversal."""
    now = now or timezone.now()
    supplied_requests = requests is not None
    if requests is None:
        requests = _request_queryset(institution=institution, now=now)
    requests_list = list(requests)
    eligible = _filter_graph_eligible_requests(
        institution=institution,
        requests=requests_list,
        infer_institution=supplied_requests,
        now=now,
        status_adapter=status_adapter,
    )
    return _build_exchange_graph_from_eligible(eligible)


def _filter_graph_eligible_requests(
    *,
    institution,
    requests: Iterable[ExchangeRequest],
    infer_institution: bool = True,
    now,
    status_adapter: ExternalEligibilityAdapter | None = None,
) -> list[ExchangeRequest]:
    """Resolve policies/reservations and retain only eligible graph vertices."""
    requests_list = list(requests)

    reserved_request_ids = set()
    configured_policies = {}

    target_institution = institution
    if target_institution is None and infer_institution:
        first_req = next(iter(requests_list), None)
        if first_req and getattr(first_req, "institution", None):
            target_institution = first_req.institution

    if target_institution is not None:
        from .services import get_institution_exchange_policies

        configured_policies = get_institution_exchange_policies(target_institution)
        reserved_request_ids = set(
            ProposalParticipant.objects.filter(
                proposal__institution=target_institution,
                is_active_reservation=True,
            ).values_list("exchange_request_id", flat=True)
        )

    return [
        request
        for request in requests_list
        if _request_is_graph_eligible(
            request,
            now=now,
            reserved_request_ids=reserved_request_ids,
            configured_policies=configured_policies,
            status_adapter=status_adapter,
        )
    ]


def _build_exchange_graph_from_eligible(
    eligible: Iterable[ExchangeRequest],
) -> ExchangeGraph:
    """Create indexed graph edges from requests whose eligibility is established."""
    eligible = list(eligible)
    vertices = {str(request.id): request for request in eligible}
    edges = {request_id: [] for request_id in vertices}
    eligible_items = list(vertices.items())
    all_candidate_ids = set(vertices)
    snapshots = {
        request_id: _request_snapshot(request)
        for request_id, request in eligible_items
    }

    indexes = {
        "resident_category": {},
        "hostel": {},
        "bed": {},
        "block": {},
        "floor": {},
        "room_type": {},
        "occupancy_category": {},
        "accessibility": {},
    }

    def add_index(index_name, key, candidate_id):
        if key is None or key == "":
            return
        indexes[index_name].setdefault(key, set()).add(candidate_id)

    for candidate_id, candidate in snapshots.items():
        bed = candidate.current_bed
        add_index("resident_category", bed.hostel_resident_category, candidate_id)
        add_index("hostel", bed.hostel_id, candidate_id)
        add_index("bed", bed.bed_id, candidate_id)
        add_index("block", bed.block_id, candidate_id)
        add_index("floor", bed.floor_id, candidate_id)
        add_index("room_type", bed.room_type, candidate_id)
        add_index(
            "occupancy_category",
            bed.occupancy_category_normalised,
            candidate_id,
        )
        for ngram in _accessibility_ngrams(bed.accessibility_normalised):
            add_index("accessibility", ngram, candidate_id)

    def candidate_ids_for(request):
        room_preferences = [
            ("resident_category", request.student_residential_eligibility),
            ("bed", request.preferred_bed_id),
            ("hostel", request.preferred_hostel_id),
            ("block", request.preferred_block_id),
            ("floor", request.preferred_floor_id),
            ("room_type", request.preferred_room_type),
            (
                "occupancy_category",
                request.preferred_occupancy_category_normalised,
            ),
        ]
        candidate_sets = [
            indexes[index_name].get(value, set())
            for index_name, value in room_preferences
            if value not in (None, "")
        ]
        for token in request.preferred_accessibility_tokens:
            if len(token) < 3:
                candidate_sets.append(all_candidate_ids)
            else:
                candidate_sets.append(
                    indexes["accessibility"].get(token[:3], set())
                )
        if any(not candidate_set for candidate_set in candidate_sets):
            return ()
        if not candidate_sets:
            return tuple(sorted(all_candidate_ids))
        return tuple(sorted(set.intersection(*candidate_sets)))

    for request_id, request in snapshots.items():
        request_edges = edges[request_id]
        for candidate_id in candidate_ids_for(request):
            if request_id == candidate_id:
                continue
            candidate = snapshots[candidate_id]
            # Exclude self-matches and same-bed targets
            if request.student_id == candidate.student_id:
                continue
            if request.current_bed.bed_id == candidate.current_bed.bed_id:
                continue

            if _snapshot_satisfies_request(request, candidate.current_bed):
                request_edges.append(candidate_id)
        request_edges.sort()

    return ExchangeGraph(
        vertices=vertices,
        edges={request_id: tuple(request_edges) for request_id, request_edges in edges.items()},
        snapshots=snapshots,
    )


class CycleList(list):
    """List of ExchangeCycle instances carrying a limit_reached flag."""

    def __init__(self, iterable=(), limit_reached: bool = False):
        super().__init__(iterable)
        self.limit_reached = limit_reached


def find_exchange_cycles(
    graph: ExchangeGraph,
    *,
    min_length: int = 2,
    max_length: int = 5,
    max_cycles: int | None = 10_000,
) -> CycleList:
    """
    Find canonical simple directed cycles of length between min_length and max_length.

    Canonical representation starts at the minimum request ID in the cycle.
    Restricting intermediate search nodes to candidate > start ensures that:
    1. Every directed cycle is found exactly once at its lexicographic minimum.
    2. Zero duplicate rotations are generated.
    3. Distinct directed cycles over the same vertices remain distinct.

    Returns:
        CycleList of ExchangeCycle instances, with .limit_reached attribute.
    """
    if min_length < 2 or max_length > 5 or min_length > max_length:
        raise ValueError("Cycle lengths must be within the inclusive range 2 to 5.")

    cycles: list[ExchangeCycle] = []
    limit_reached = False
    sorted_vertices = sorted(graph.vertices.keys())

    for start in sorted_vertices:
        path = [start]
        path_set = {start}

        def visit(current: str):
            nonlocal limit_reached
            if max_cycles is not None and len(cycles) >= max_cycles:
                limit_reached = True
                return

            for candidate in graph.edges.get(current, ()):
                if candidate == start:
                    if min_length <= len(path) <= max_length:
                        # Defensive check: ensure all students and beds in the cycle are distinct
                        cycle_reqs = [graph.vertices[rid] for rid in path]
                        students = {r.student_id for r in cycle_reqs}
                        beds = {r.current_occupancy.bed_id for r in cycle_reqs}
                        if len(students) == len(path) and len(beds) == len(path):
                            cycles.append(ExchangeCycle(tuple(path)))
                            if max_cycles is not None and len(cycles) >= max_cycles:
                                limit_reached = True
                                return
                    continue

                if candidate in path_set or candidate < start or len(path) >= max_length:
                    continue

                path.append(candidate)
                path_set.add(candidate)
                visit(candidate)
                path.pop()
                path_set.remove(candidate)
                if limit_reached:
                    return

        visit(start)
        if limit_reached:
            break

    return CycleList(cycles, limit_reached=limit_reached)



def _build_candidate_matches_with_timing(
    graph: ExchangeGraph,
    cycles: list[ExchangeCycle],
    *,
    now: datetime | None = None,
) -> tuple[list[CandidateMatch], float]:
    """Transform detected ExchangeCycles into structured, explanatory CandidateMatch objects."""
    now = now or timezone.now()
    results = []
    explanation_seconds = 0.0
    snapshots = graph.snapshots or {}

    for cycle in cycles:
        k = len(cycle.request_ids)
        participants = []
        hostel_ids = set()

        for idx, request_id in enumerate(cycle.request_ids):
            req = graph.vertices[request_id]
            next_req = graph.vertices[cycle.request_ids[(idx + 1) % k]]
            request_snapshot = snapshots.get(request_id)
            next_snapshot = snapshots.get(cycle.request_ids[(idx + 1) % k])

            explanation_start = perf_counter()
            if request_snapshot is not None and next_snapshot is not None:
                current_bed = request_snapshot.current_bed
                proposed_bed = next_snapshot.current_bed
                explanation = _explain_preference_snapshot(
                    request_snapshot,
                    proposed_bed,
                )
                student_name = request_snapshot.student_name
                student_registration = request_snapshot.student_registration
                current_occupancy_id = request_snapshot.current_occupancy_id
            else:
                curr_occ = req.current_occupancy
                current_bed_model = curr_occ.bed
                current_bed = _bed_snapshot(current_bed_model)
                proposed_bed_model = next_req.current_occupancy.bed
                proposed_bed = _bed_snapshot(proposed_bed_model)
                explanation = explain_preference_match(req, proposed_bed_model)
                student_name = req.student.full_name
                student_registration = getattr(req.student, "registration_number", "") or ""
                current_occupancy_id = curr_occ.id
            explanation_seconds += perf_counter() - explanation_start

            hostel_ids.add(current_bed.hostel_id)
            hostel_ids.add(proposed_bed.hostel_id)

            participants.append(
                CandidateParticipant(
                    request_id=str(req.id),
                    student_id=req.student_id,
                    student_name=student_name,
                    student_registration=student_registration,
                    current_occupancy_id=current_occupancy_id,
                    current_bed_id=current_bed.bed_id,
                    current_bed_label=current_bed.bed_label,
                    current_room_number=current_bed.room_number,
                    current_hostel_id=current_bed.hostel_id,
                    current_hostel_name=current_bed.hostel_name,
                    proposed_bed_id=proposed_bed.bed_id,
                    proposed_bed_label=proposed_bed.bed_label,
                    proposed_room_number=proposed_bed.room_number,
                    proposed_hostel_id=proposed_bed.hostel_id,
                    proposed_hostel_name=proposed_bed.hostel_name,
                    preference_match_explanation=explanation,
                )
            )

        is_cross = len(hostel_ids) > 1
        results.append(
            CandidateMatch(
                cycle_length=k,
                is_cross_hostel=is_cross,
                classification="cross_hostel" if is_cross else "same_hostel",
                participants=tuple(participants),
                request_ids=cycle.request_ids,
                cycle_signature=cycle.signature,
                detected_at=now,
            )
        )

    return results, explanation_seconds


def build_candidate_matches(
    graph: ExchangeGraph,
    cycles: list[ExchangeCycle],
    *,
    now: datetime | None = None,
) -> list[CandidateMatch]:
    """Transform cycles into candidate matches while retaining the public API."""
    candidates, _ = _build_candidate_matches_with_timing(graph, cycles, now=now)
    return candidates


@transaction.atomic
def persist_candidate_matches(
    candidates: list[CandidateMatch],
    *,
    institution,
    graph: ExchangeGraph | None = None,
    now: datetime | None = None,
) -> list[ExchangeProposal]:
    """
    Persist candidate matches as draft ExchangeProposals without locking requests or modifying occupancies.

    Re-checks request and occupancy states from the database before storing. Idempotent across repeated runs.
    """
    now = now or timezone.now()
    created_proposals = []

    existing_signatures = set(
        ExchangeProposal.objects.filter(
            institution=institution,
            status=ExchangeProposal.Status.DRAFT,
            cycle_signature__isnull=False,
        ).values_list("cycle_signature", flat=True)
    )

    for candidate in candidates:
        if candidate.cycle_signature in existing_signatures:
            continue

        req_ids = list(candidate.request_ids)
        current_reqs = ExchangeRequest.objects.select_for_update().filter(
            id__in=req_ids,
            institution=institution,
            status=ExchangeRequest.Status.OPEN,
        ).filter(models_q_expiry(now))

        if current_reqs.count() != len(req_ids):
            continue

        req_map = {str(r.id): r for r in current_reqs}
        valid_occupancies = True
        for part in candidate.participants:
            req = req_map.get(part.request_id)
            if not req or req.current_occupancy_id != part.current_occupancy_id:
                valid_occupancies = False
                break
            if req.current_occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE:
                valid_occupancies = False
                break
            if req.current_occupancy.bed_id != part.current_bed_id:
                valid_occupancies = False
                break

        if not valid_occupancies:
            continue

        expiry_values = [
            req.expires_at
            for req in req_map.values()
            if req.expires_at is not None
        ]
        earliest_expiry = min(expiry_values) if expiry_values else None

        try:
            with transaction.atomic():
                proposal = ExchangeProposal.objects.create(
                    institution=institution,
                    status=ExchangeProposal.Status.DRAFT,
                    cycle_length=candidate.cycle_length,
                    cycle_signature=candidate.cycle_signature,
                    expires_at=earliest_expiry,
                    matched_at=now,
                )
                for seq_order, part in enumerate(candidate.participants):
                    req = req_map[part.request_id]
                    ProposalParticipant.objects.create(
                        proposal=proposal,
                        exchange_request=req,
                        student=req.student,
                        current_occupancy=req.current_occupancy,
                        proposed_bed_id=part.proposed_bed_id,
                        sequence_order=seq_order,
                        is_active_reservation=False,  # Candidates do NOT lock active reservations
                    )
                # Note: ExchangeRequest.status is strictly preserved as OPEN.
                created_proposals.append(proposal)
                existing_signatures.add(candidate.cycle_signature)
        except IntegrityError:
            continue

    return created_proposals


def find_candidate_matches(
    *,
    institution,
    now: datetime | None = None,
    status_adapter: ExternalEligibilityAdapter | None = None,
    min_length: int = 2,
    max_length: int | None = None,
    max_cycles: int | None = 10_000,
    persist_candidates: bool = False,
) -> MatchingEngineResult:
    """
    Core matching engine service.
    Exposes candidate detection for direct swaps and 3-5 person rotation cycles.
    """
    t_start = perf_counter()
    now = now or timezone.now()

    t_policy_start = perf_counter()
    max_length = get_effective_max_cycle_length(institution, max_length)
    policy_resolution_time = perf_counter() - t_policy_start

    t_load_start = perf_counter()
    requests_qs = _request_queryset(institution=institution, now=now)
    requests_list = list(requests_qs)
    t_load_end = perf_counter()
    data_loading_time = t_load_end - t_load_start

    t_eligibility_start = perf_counter()
    eligible_requests = _filter_graph_eligible_requests(
        institution=institution,
        requests=requests_list,
        infer_institution=True,
        now=now,
        status_adapter=status_adapter,
    )
    eligibility_evaluation_time = perf_counter() - t_eligibility_start

    t_build_start = perf_counter()
    graph = _build_exchange_graph_from_eligible(eligible_requests)
    graph_building_time = perf_counter() - t_build_start

    t_cycle_start = perf_counter()
    cycles = find_exchange_cycles(
        graph,
        min_length=min_length,
        max_length=max_length,
        max_cycles=max_cycles,
    )
    limit_reached = getattr(cycles, "limit_reached", False)
    t_cycle_end = perf_counter()
    cycle_detection_time = t_cycle_end - t_cycle_start

    t_candidate_start = perf_counter()
    candidates, explanation_generation_time = _build_candidate_matches_with_timing(
        graph,
        cycles,
        now=now,
    )
    candidate_materialization_time = perf_counter() - t_candidate_start
    total_time = perf_counter() - t_start

    candidates_by_len = {}
    same_hostel_cnt = 0
    cross_hostel_cnt = 0
    for cand in candidates:
        candidates_by_len[cand.cycle_length] = candidates_by_len.get(cand.cycle_length, 0) + 1
        if cand.is_cross_hostel:
            cross_hostel_cnt += 1
        else:
            same_hostel_cnt += 1

    total_edges = sum(len(e) for e in graph.edges.values())
    created_proposals = []
    persistence_time = 0.0
    if persist_candidates and candidates and institution:
        persistence_start = perf_counter()
        created_proposals = persist_candidate_matches(
            candidates,
            institution=institution,
            graph=graph,
            now=now,
        )
        persistence_time = perf_counter() - persistence_start
    total_with_persistence_time = perf_counter() - t_start

    metrics = MatchingMetrics(
        graph_nodes=len(graph.vertices),
        graph_edges=total_edges,
        total_candidates=len(candidates),
        candidates_by_length=candidates_by_len,
        same_hostel_count=same_hostel_cnt,
        cross_hostel_count=cross_hostel_cnt,
        policy_resolution_time_seconds=policy_resolution_time,
        data_loading_time_seconds=data_loading_time,
        eligibility_evaluation_time_seconds=eligibility_evaluation_time,
        graph_building_time_seconds=graph_building_time,
        cycle_detection_time_seconds=cycle_detection_time,
        candidate_materialization_time_seconds=candidate_materialization_time,
        explanation_generation_time_seconds=explanation_generation_time,
        persistence_time_seconds=persistence_time,
        total_time_seconds=total_time,
        total_with_persistence_time_seconds=total_with_persistence_time,
        limit_reached=limit_reached,
    )

    inst_id = institution.id if institution else 0
    inst_name = institution.name if institution else "All Institutions"

    return MatchingEngineResult(
        institution_id=inst_id,
        institution_name=inst_name,
        candidates=candidates,
        metrics=metrics,
        proposals_created=created_proposals,
    )


# Backward compatibility helpers
def _request_preferences(request):
    return {
        "preferred_hostel": request.preferred_hostel,
        "preferred_bed": request.preferred_bed,
        "preferred_block": request.preferred_block,
        "preferred_floor": request.preferred_floor,
        "preferred_room_type": request.preferred_room_type,
        "preferred_occupancy_category": request.preferred_occupancy_category,
        "preferred_accessibility_attributes": request.preferred_accessibility_attributes,
        "reason": request.reason,
    }


def _revalidate_cycle(*, cycle, graph, status_adapter=None, today=None):
    for request_id in cycle.request_ids:
        request = graph.vertices[request_id]
        validate_exchange_request_eligibility(
            student=request.student,
            occupancy=request.current_occupancy,
            preferences=_request_preferences(request),
            status_adapter=status_adapter,
            today=today,
        )
    return True


@transaction.atomic
def create_exchange_proposals(
    *,
    institution=None,
    now=None,
    status_adapter: ExternalEligibilityAdapter | None = None,
    max_proposals: int | None = None,
    max_cycles: int | None = 10_000,
) -> list[ExchangeProposal]:
    """
    Active proposal generation helper (for backward compatibility).
    """
    now = now or timezone.now()
    result = find_candidate_matches(
        institution=institution,
        now=now,
        status_adapter=status_adapter,
        max_cycles=max_cycles,
    )

    reserved_request_ids = set(
        ProposalParticipant.objects.filter(
            is_active_reservation=True,
        ).values_list("exchange_request_id", flat=True)
    )
    cycle_signatures = set(
        ExchangeProposal.objects.filter(
            status__in=ACTIVE_PROPOSAL_STATUSES,
            cycle_signature__isnull=False,
        ).values_list("cycle_signature", flat=True)
    )
    proposals = []

    for candidate in result.candidates:
        if max_proposals is not None and len(proposals) >= max_proposals:
            break
        if candidate.cycle_signature in cycle_signatures:
            continue
        if any(rid in reserved_request_ids for rid in candidate.request_ids):
            continue

        try:
            with transaction.atomic():
                first_req = ExchangeRequest.objects.get(id=candidate.request_ids[0])
                proposal = ExchangeProposal.objects.create(
                    institution=first_req.institution,
                    status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
                    cycle_length=candidate.cycle_length,
                    cycle_signature=candidate.cycle_signature,
                    matched_at=now,
                )
                for seq_order, part in enumerate(candidate.participants):
                    req = ExchangeRequest.objects.get(id=part.request_id)
                    ProposalParticipant.objects.create(
                        proposal=proposal,
                        exchange_request=req,
                        student=req.student,
                        current_occupancy=req.current_occupancy,
                        proposed_bed_id=part.proposed_bed_id,
                        sequence_order=seq_order,
                        is_active_reservation=True,
                    )
                ExchangeRequest.objects.filter(
                    id__in=candidate.request_ids,
                ).update(status=ExchangeRequest.Status.MATCHED)
                proposals.append(proposal)
                cycle_signatures.add(candidate.cycle_signature)
                reserved_request_ids.update(candidate.request_ids)
        except IntegrityError:
            continue

    return proposals


match_exchange_requests = find_candidate_matches
