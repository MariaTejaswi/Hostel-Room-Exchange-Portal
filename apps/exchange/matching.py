from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.hostels.models import Hostel, OperationalStatus, StudentOccupancy

from .eligibility import ExternalEligibilityAdapter, validate_exchange_request_eligibility
from .models import ExchangeProposal, ExchangeRequest, ProposalParticipant


ACTIVE_PROPOSAL_STATUSES = (
    ExchangeProposal.Status.DRAFT,
    ExchangeProposal.Status.PENDING_ACCEPTANCE,
    ExchangeProposal.Status.ACCEPTED,
    ExchangeProposal.Status.PENDING_APPROVAL,
    ExchangeProposal.Status.APPROVED,
)


@dataclass(frozen=True)
class ExchangeGraph:
    """Directed request graph; an edge A -> B means A can receive B's room."""

    vertices: dict[str, ExchangeRequest]
    edges: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class ExchangeCycle:
    """A directed cycle in participant order."""

    request_ids: tuple[str, ...]

    @property
    def signature(self) -> str:
        return ",".join(sorted(self.request_ids))


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


def _request_is_graph_eligible(request, *, now: datetime) -> bool:
    if request.status != ExchangeRequest.Status.OPEN:
        return False
    if request.expires_at is not None and request.expires_at <= now:
        return False
    occupancy = request.current_occupancy
    if (
        occupancy.status != StudentOccupancy.OccupancyStatus.ACTIVE
        or occupancy.student_id != request.student_id
        or occupancy.institution_id != request.institution_id
    ):
        return False
    bed = occupancy.bed
    room = bed.room
    floor = room.floor
    block = floor.block
    hostel = block.hostel
    if (
        hostel.institution_id != request.institution_id
        or hostel.status != Hostel.Status.ACTIVE
        or hostel.resident_category != request.student.residential_eligibility
        or not block.is_active
        or not floor.is_active
        or not room.is_active
        or room.operational_status != OperationalStatus.IN_SERVICE
        or not bed.is_active
        or bed.operational_status != OperationalStatus.IN_SERVICE
    ):
        return False
    return request.student.residential_eligibility in {
        Hostel.ResidentCategory.MALE,
        Hostel.ResidentCategory.FEMALE,
    }


def _request_queryset(*, institution=None, now=None):
    now = now or timezone.now()
    queryset = ExchangeRequest.objects.filter(status=ExchangeRequest.Status.OPEN).filter(
        models_q_expiry(now)
    )
    if institution is not None:
        queryset = queryset.filter(institution=institution)
    return queryset.select_related(
        "student",
        "institution",
        "current_occupancy__bed__room__floor__block__hostel",
        "preferred_hostel",
        "preferred_bed",
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
) -> ExchangeGraph:
    """Load eligible open requests and create preference-compatible directed edges."""
    now = now or timezone.now()
    if requests is None:
        requests = _request_queryset(institution=institution, now=now)
    eligible = [
        request
        for request in requests
        if _request_is_graph_eligible(request, now=now)
    ]
    vertices = {str(request.id): request for request in eligible}
    edges = {request_id: [] for request_id in vertices}
    eligible_items = list(vertices.items())
    all_candidate_ids = set(vertices)
    indexes = {
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

    for candidate_id, candidate in eligible_items:
        room = candidate.current_occupancy.bed.room
        floor = room.floor
        block = floor.block
        hostel = block.hostel
        add_index("hostel", hostel.id, candidate_id)
        add_index("bed", candidate.current_occupancy.bed_id, candidate_id)
        add_index("block", block.id, candidate_id)
        add_index("floor", floor.id, candidate_id)
        add_index("room_type", room.room_type, candidate_id)
        add_index(
            "occupancy_category",
            _normalise_text(room.occupancy_category),
            candidate_id,
        )
        for ngram in _accessibility_ngrams(room.accessibility_attributes):
            add_index("accessibility", ngram, candidate_id)

    def candidate_ids_for(request):
        room_preferences = [
            ("bed", request.preferred_bed_id),
            ("hostel", request.preferred_hostel_id),
            ("block", request.preferred_block_id),
            ("floor", request.preferred_floor_id),
            ("room_type", request.preferred_room_type),
            (
                "occupancy_category",
                _normalise_text(request.preferred_occupancy_category),
            ),
        ]
        candidate_sets = [
            indexes[index_name].get(value, set())
            for index_name, value in room_preferences
            if value not in (None, "")
        ]
        for token in _accessibility_tokens(
            request.preferred_accessibility_attributes
        ):
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

    for request_id, request in eligible_items:
        request_edges = edges[request_id]
        for candidate_id in candidate_ids_for(request):
            if request_id == candidate_id:
                continue
            candidate = vertices[candidate_id]
            if room_satisfies_request(
                request=request,
                bed=candidate.current_occupancy.bed,
            ):
                request_edges.append(candidate_id)
        request_edges.sort()

    return ExchangeGraph(
        vertices=vertices,
        edges={request_id: tuple(request_edges) for request_id, request_edges in edges.items()},
    )


def find_exchange_cycles(
    graph: ExchangeGraph,
    *,
    min_length: int = 2,
    max_length: int = 5,
    max_cycles: int | None = 10_000,
) -> list[ExchangeCycle]:
    """Find canonical simple directed cycles of lengths two through five."""
    if min_length < 2 or max_length > 5 or min_length > max_length:
        raise ValueError("Cycle lengths must be within the inclusive range 2 to 5.")

    cycles = []
    for start in sorted(graph.vertices):
        path = [start]

        def visit(current):
            if max_cycles is not None and len(cycles) >= max_cycles:
                return
            for candidate in graph.edges.get(current, ()):
                if candidate == start:
                    if min_length <= len(path) <= max_length:
                        cycles.append(ExchangeCycle(tuple(path)))
                    continue
                if candidate in path or candidate < start or len(path) >= max_length:
                    continue
                path.append(candidate)
                visit(candidate)
                path.pop()

        visit(start)
        if max_cycles is not None and len(cycles) >= max_cycles:
            break
    return cycles


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


def _request_ids_in_active_reservations():
    return set(
        ProposalParticipant.objects.filter(
            is_active_reservation=True,
        ).values_list("exchange_request_id", flat=True)
    )


def _active_cycle_signatures():
    return set(
        ExchangeProposal.objects.filter(
            status__in=ACTIVE_PROPOSAL_STATUSES,
            cycle_signature__isnull=False,
        ).values_list("cycle_signature", flat=True)
    )


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
    Match open requests into unique pending-acceptance proposals.

    Matching only reserves proposal participants. It does not accept proposals,
    approve them, or change any room or occupancy record.
    """
    now = now or timezone.now()
    locked_queryset = ExchangeRequest.objects.select_for_update().filter(
        status=ExchangeRequest.Status.OPEN,
    ).filter(models_q_expiry(now))
    if institution is not None:
        locked_queryset = locked_queryset.filter(institution=institution)
    locked_requests = list(locked_queryset.values_list("id", flat=True))
    if not locked_requests:
        return []

    graph = build_exchange_graph(
        institution=institution,
        requests=_request_queryset(institution=institution, now=now).filter(
            id__in=locked_requests,
        ),
        now=now,
    )
    cycles = find_exchange_cycles(graph, max_cycles=max_cycles)
    reserved_request_ids = _request_ids_in_active_reservations()
    cycle_signatures = _active_cycle_signatures()
    proposals = []

    for cycle in cycles:
        if max_proposals is not None and len(proposals) >= max_proposals:
            break
        if cycle.signature in cycle_signatures:
            continue
        if any(request_id in reserved_request_ids for request_id in cycle.request_ids):
            continue
        try:
            _revalidate_cycle(
                cycle=cycle,
                graph=graph,
                status_adapter=status_adapter,
                today=now.date(),
            )
        except ValidationError:
            continue

        cycle_requests = [graph.vertices[request_id] for request_id in cycle.request_ids]
        expiry_values = [
            request.expires_at
            for request in cycle_requests
            if request.expires_at is not None
        ]
        try:
            with transaction.atomic():
                proposal = ExchangeProposal.objects.create(
                    institution=cycle_requests[0].institution,
                    status=ExchangeProposal.Status.PENDING_ACCEPTANCE,
                    cycle_length=len(cycle.request_ids),
                    cycle_signature=cycle.signature,
                    expires_at=min(expiry_values) if expiry_values else None,
                    matched_at=now,
                )
                for sequence_order, request_id in enumerate(cycle.request_ids):
                    request = graph.vertices[request_id]
                    destination_request = graph.vertices[
                        cycle.request_ids[(sequence_order + 1) % len(cycle.request_ids)]
                    ]
                    participant = ProposalParticipant(
                        proposal=proposal,
                        exchange_request=request,
                        student=request.student,
                        current_occupancy=request.current_occupancy,
                        proposed_bed=destination_request.current_occupancy.bed,
                        sequence_order=sequence_order,
                    )
                    participant.full_clean()
                    participant.save()
                ExchangeRequest.objects.filter(
                    id__in=cycle.request_ids,
                ).update(status=ExchangeRequest.Status.MATCHED)
        except IntegrityError:
            continue

        proposals.append(proposal)
        cycle_signatures.add(cycle.signature)
        reserved_request_ids.update(cycle.request_ids)

    return proposals


match_exchange_requests = create_exchange_proposals
