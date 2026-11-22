from datetime import datetime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_CHIEF_WARDEN,
    ROLE_DEAN,
    ROLE_HOSTEL_ADMIN,
    ROLE_STUDENT,
    ROLE_SYSADMIN,
    ROLE_WARDEN,
    create_user_with_role,
)
from apps.core.models import AuditLog
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    ProposalParticipant,
    TransferRecord,
)
from apps.exchange.reporting import (
    build_exchange_report,
    get_exchange_audit_queryset,
    make_report_window,
)
from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
    WardenAssignment,
)


KOLKATA = ZoneInfo("Asia/Kolkata")


def _at(day, hour=12, minute=0):
    return datetime(2026, 1, day, hour, minute, tzinfo=KOLKATA)


def _make_user(*, institution, email, name, role, registration_number=None):
    return create_user_with_role(
        email=email,
        full_name=name,
        role_name=role,
        institution=institution,
        password="M7-Test-Password-123!",
        registration_number=registration_number,
        residential_eligibility=(
            User.ResidentialEligibility.MALE
            if role == ROLE_STUDENT
            else User.ResidentialEligibility.UNVERIFIED
        ),
        is_staff=role in {ROLE_HOSTEL_ADMIN, ROLE_SYSADMIN},
    )


def _make_hostel(institution, code, name):
    hostel = Hostel.objects.create(
        institution=institution,
        code=code,
        name=name,
        resident_category=Hostel.ResidentCategory.MALE,
    )
    block = Block.objects.create(hostel=hostel, name="Block A", code=f"{code}-A")
    floor = Floor.objects.create(block=block, floor_number=1, floor_label="First")
    room = Room.objects.create(
        floor=floor,
        room_number="101",
        room_type=Room.RoomType.DOUBLE,
        operational_status=OperationalStatus.IN_SERVICE,
    )
    beds = [
        Bed.objects.create(
            room=room,
            bed_label=f"Bed {index}",
            operational_status=OperationalStatus.IN_SERVICE,
        )
        for index in range(1, 13)
    ]
    return hostel, room, beds


def _set_proposal_created_at(proposal, value):
    ExchangeProposal.objects.filter(pk=proposal.pk).update(
        created_at=value,
        updated_at=value,
    )
    proposal.created_at = value


def _audit(*, institution, action, target, timestamp, actor=None, summary=""):
    event = AuditLog.objects.create(
        institution=institution,
        acting_user=actor,
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        change_summary=summary,
    )
    AuditLog.objects.filter(pk=event.pk).update(timestamp=timestamp)
    event.timestamp = timestamp
    return event


@pytest.fixture
def reporting_dataset(institution, other_institution, roles):
    dean = _make_user(
        institution=institution,
        email="m7.dean@example.test",
        name="M7 Dean",
        role=ROLE_DEAN,
    )
    chief = _make_user(
        institution=institution,
        email="m7.chief@example.test",
        name="M7 Chief Warden",
        role=ROLE_CHIEF_WARDEN,
    )
    warden = _make_user(
        institution=institution,
        email="m7.warden@example.test",
        name="M7 Warden",
        role=ROLE_WARDEN,
    )
    admin = _make_user(
        institution=institution,
        email="m7.admin@example.test",
        name="M7 Hostel Administrator",
        role=ROLE_HOSTEL_ADMIN,
    )
    sysadmin = _make_user(
        institution=institution,
        email="m7.sysadmin@example.test",
        name="M7 System Administrator",
        role=ROLE_SYSADMIN,
    )

    hostel_a, _room_a, beds_a = _make_hostel(institution, "M7-A", "M7 North")
    hostel_b, _room_b, beds_b = _make_hostel(institution, "M7-B", "M7 South")
    WardenAssignment.objects.create(warden=warden, hostel=hostel_a, is_primary=True)

    students = [
        _make_user(
            institution=institution,
            email=f"m7.student{index}@example.test",
            name=f"M7 Student {index}",
            role=ROLE_STUDENT,
            registration_number=f"M7-{index:03d}",
        )
        for index in range(9)
    ]
    source_beds = [
        beds_a[0],
        beds_a[1],
        beds_b[0],
        beds_a[2],
        beds_a[3],
        beds_a[4],
        beds_a[5],
        beds_b[1],
        beds_a[6],
    ]
    occupancies = [
        StudentOccupancy.objects.create(
            institution=institution,
            student=student,
            bed=bed,
            start_date=datetime(2025, 8, 1).date(),
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        for student, bed in zip(students, source_beds, strict=True)
    ]

    def request(
        index, submitted_at, status, *, preferred_bed=None, request_type="exchange"
    ):
        return ExchangeRequest.objects.create(
            institution=institution,
            student=students[index],
            current_occupancy=occupancies[index],
            request_type=request_type,
            preferred_bed=preferred_bed,
            reason=f"M7 deterministic request {index}",
            status=status,
            submitted_at=submitted_at,
        )

    requests = {
        "boundary": request(0, _at(1, 0), ExchangeRequest.Status.OPEN),
        "direct_a": request(
            1,
            _at(10),
            ExchangeRequest.Status.COMPLETED,
            preferred_bed=beds_b[8],
        ),
        "direct_b": request(2, _at(11), ExchangeRequest.Status.COMPLETED),
        "multi_a": request(
            3,
            _at(12),
            ExchangeRequest.Status.COMPLETED,
            preferred_bed=beds_a[10],
        ),
        "multi_b": request(4, _at(13), ExchangeRequest.Status.COMPLETED),
        "vacant": request(
            5,
            _at(14),
            ExchangeRequest.Status.COMPLETED,
            preferred_bed=beds_b[9],
            request_type=ExchangeRequest.RequestType.VACANT_TRANSFER,
        ),
        "multi_c": request(6, _at(15), ExchangeRequest.Status.COMPLETED),
        "end_boundary": request(7, _at(31, 23, 59), ExchangeRequest.Status.EXPIRED),
    }

    def proposal(
        name, status, cycle_length, created_at, matched_at=None, completed_at=None
    ):
        value = ExchangeProposal.objects.create(
            institution=institution,
            status=status,
            cycle_length=cycle_length,
            matched_at=matched_at,
            completed_at=completed_at,
        )
        _set_proposal_created_at(value, created_at)
        proposals[name] = value
        return value

    def participants(value, request_keys, destination_beds):
        result = []
        for sequence, (request_key, destination_bed) in enumerate(
            zip(request_keys, destination_beds, strict=True)
        ):
            exchange_request = requests[request_key]
            result.append(
                ProposalParticipant.objects.create(
                    proposal=value,
                    exchange_request=exchange_request,
                    student=exchange_request.student,
                    current_occupancy=exchange_request.current_occupancy,
                    proposed_bed=destination_bed,
                    sequence_order=sequence,
                    response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
                    is_active_reservation=False,
                )
            )
        return result

    proposals = {}
    direct = proposal(
        "direct",
        ExchangeProposal.Status.COMPLETED,
        2,
        _at(16),
        matched_at=_at(16),
        completed_at=_at(20),
    )
    direct_parts = participants(
        direct,
        ["direct_a", "direct_b"],
        [
            requests["direct_b"].current_occupancy.bed,
            requests["direct_a"].current_occupancy.bed,
        ],
    )
    multi = proposal(
        "multi",
        ExchangeProposal.Status.COMPLETED,
        3,
        _at(16),
        matched_at=_at(16),
        completed_at=_at(20),
    )
    multi_parts = participants(
        multi,
        ["multi_a", "multi_b", "multi_c"],
        [
            requests["multi_b"].current_occupancy.bed,
            requests["multi_c"].current_occupancy.bed,
            requests["multi_a"].current_occupancy.bed,
        ],
    )
    pending = proposal(
        "pending",
        ExchangeProposal.Status.PENDING_ACCEPTANCE,
        2,
        _at(17),
        matched_at=_at(17),
    )
    participants(pending, ["multi_a", "multi_b"], [beds_a[3], beds_a[2]])
    rejected = proposal(
        "rejected",
        ExchangeProposal.Status.REJECTED,
        2,
        _at(17),
        matched_at=_at(17),
    )
    participants(rejected, ["multi_a", "direct_b"], [beds_b[0], beds_a[2]])
    expired = proposal(
        "expired",
        ExchangeProposal.Status.EXPIRED,
        2,
        _at(18),
        matched_at=_at(18),
    )
    participants(expired, ["multi_b", "end_boundary"], [beds_b[1], beds_a[3]])
    cancelled = proposal(
        "cancelled",
        ExchangeProposal.Status.CANCELLED,
        2,
        _at(18),
        matched_at=_at(18),
    )
    participants(cancelled, ["end_boundary", "multi_a"], [beds_a[2], beds_b[1]])
    draft = proposal("draft", ExchangeProposal.Status.DRAFT, 2, _at(18))
    participants(draft, ["boundary", "end_boundary"], [beds_b[1], beds_a[0]])

    legacy = proposal(
        "legacy",
        ExchangeProposal.Status.COMPLETED,
        2,
        _at(19),
        completed_at=None,
    )
    legacy_participant = ProposalParticipant.objects.create(
        proposal=legacy,
        exchange_request=None,
        student=students[8],
        current_occupancy=occupancies[8],
        proposed_bed=beds_b[10],
        sequence_order=0,
        response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
        is_active_reservation=False,
    )

    direct_approval = Approval.objects.create(
        proposal=direct,
        authority_user=chief,
        authority_role=Approval.AuthorityRole.CHIEF_WARDEN,
        decision=Approval.Decision.APPROVED,
        decided_at=_at(20),
    )
    multi_approval = Approval.objects.create(
        proposal=multi,
        authority_user=warden,
        authority_role=Approval.AuthorityRole.WARDEN,
        scope_hostel=hostel_a,
        decision=Approval.Decision.APPROVED,
        decided_at=_at(20),
    )
    Approval.objects.create(
        proposal=rejected,
        authority_user=warden,
        authority_role=Approval.AuthorityRole.WARDEN,
        scope_hostel=hostel_a,
        decision=Approval.Decision.REJECTED,
        decided_at=_at(21),
    )
    Approval.objects.create(
        proposal=cancelled,
        authority_user=chief,
        authority_role=Approval.AuthorityRole.CHIEF_WARDEN,
        decision=Approval.Decision.REJECTED,
        decided_at=_at(21),
    )
    vacant_approval = Approval.objects.create(
        exchange_request=requests["vacant"],
        authority_user=chief,
        authority_role=Approval.AuthorityRole.CHIEF_WARDEN,
        decision=Approval.Decision.APPROVED,
        decided_at=_at(20),
    )

    def transfer(
        *, proposal_value, participant, exchange_request, approval, destination_bed
    ):
        occupancy = (
            participant.current_occupancy
            if participant
            else exchange_request.current_occupancy
        )
        student = participant.student if participant else exchange_request.student
        return TransferRecord.objects.create(
            institution=institution,
            exchange_request=exchange_request,
            proposal=proposal_value,
            participant=participant,
            approval=approval,
            approved_by=approval.authority_user if approval else chief,
            student=student,
            previous_occupancy=occupancy,
            previous_room=occupancy.bed.room,
            previous_bed=occupancy.bed,
            new_room=destination_bed.room,
            new_bed=destination_bed,
            source_hostel_name=occupancy.bed.room.floor.block.hostel.name,
            source_room_number=occupancy.bed.room.room_number,
            source_bed_label=occupancy.bed.bed_label,
            destination_hostel_name=destination_bed.room.floor.block.hostel.name,
            destination_room_number=destination_bed.room.room_number,
            destination_bed_label=destination_bed.bed_label,
            transferred_at=_at(20),
        )

    for participant in direct_parts:
        transfer(
            proposal_value=direct,
            participant=participant,
            exchange_request=participant.exchange_request,
            approval=direct_approval,
            destination_bed=participant.proposed_bed,
        )
    for participant in multi_parts:
        transfer(
            proposal_value=multi,
            participant=participant,
            exchange_request=participant.exchange_request,
            approval=multi_approval,
            destination_bed=participant.proposed_bed,
        )
    transfer(
        proposal_value=None,
        participant=None,
        exchange_request=requests["vacant"],
        approval=vacant_approval,
        destination_bed=beds_b[9],
    )
    transfer(
        proposal_value=legacy,
        participant=legacy_participant,
        exchange_request=None,
        approval=None,
        destination_bed=beds_b[10],
    )

    for key in ["multi", "pending", "rejected", "expired", "cancelled"]:
        _audit(
            institution=institution,
            action="EXCHANGE_PROPOSAL_ACTIVATED",
            target=proposals[key],
            timestamp=_at(17),
            summary="HIDDEN STUDENT ALPHA should never be rendered",
        )
    _audit(
        institution=institution,
        action="EXCHANGE_PROPOSAL_APPROVED",
        target=direct,
        timestamp=_at(20),
        actor=chief,
        summary="HIDDEN STUDENT ALPHA approved",
    )
    _audit(
        institution=institution,
        action="EXCHANGE_PROPOSAL_APPROVED",
        target=direct,
        timestamp=_at(20, 12, 1),
        actor=chief,
        summary="Duplicate retry audit evidence; no second Approval row",
    )
    _audit(
        institution=institution,
        action="EXCHANGE_TRANSFER_EXECUTED",
        target=multi,
        timestamp=_at(20),
        actor=warden,
    )

    other_hostel, _, other_beds = _make_hostel(
        other_institution,
        "M7-O",
        "Other Institution Hostel",
    )
    other_student = _make_user(
        institution=other_institution,
        email="m7.other.student@example.test",
        name="Other Institution Student",
        role=ROLE_STUDENT,
        registration_number="M7-OTHER",
    )
    other_occupancy = StudentOccupancy.objects.create(
        institution=other_institution,
        student=other_student,
        bed=other_beds[0],
        start_date=datetime(2025, 8, 1).date(),
    )
    other_request = ExchangeRequest.objects.create(
        institution=other_institution,
        student=other_student,
        current_occupancy=other_occupancy,
        reason="Must remain institution-isolated",
        submitted_at=_at(10),
    )
    _audit(
        institution=other_institution,
        action="EXCHANGE_REQUEST_CREATED",
        target=other_request,
        timestamp=_at(10),
    )

    return {
        "dean": dean,
        "chief": chief,
        "warden": warden,
        "admin": admin,
        "sysadmin": sysadmin,
        "student": students[0],
        "hostel_a": hostel_a,
        "hostel_b": hostel_b,
        "other_hostel": other_hostel,
        "window": make_report_window(
            datetime(2026, 1, 1).date(), datetime(2026, 1, 31).date()
        ),
        "institution": institution,
    }


@pytest.mark.django_db
def test_reporting_metrics_are_exact_and_keep_workflow_units_distinct(
    reporting_dataset,
):
    data = reporting_dataset
    report = build_exchange_report(user=data["dean"], window=data["window"])

    assert report["request_summary"] == {
        "total": 8,
        "exchange": 7,
        "vacant_transfer": 1,
        "broad_preferences": 5,
        "explicit_targets": 3,
    }
    assert {
        row["hostel_name"]: row["request_count"] for row in report["volume_by_hostel"]
    } == {"M7 North": 6, "M7 South": 2}
    assert report["match"]["matched_requests"] == 6
    assert report["match"]["eligible_requests"] == 7
    assert report["match"]["rate"] == pytest.approx(85.7142857)

    assert report["workflow"]["draft_candidates"] == 1
    assert report["workflow"]["activated_proposals"] == 6
    assert report["workflow"]["activation_event_count"] == 5
    assert report["workflow"]["activation_fallback_count"] == 1
    assert report["workflow"]["missing_activation_history_count"] == 1

    target_counts = {
        (row["hostel_name"], row["room_number"]): row["request_count"]
        for row in report["targets"]["explicit_rooms"]
    }
    assert target_counts == {("M7 South", "101"): 2, ("M7 North", "101"): 1}

    patterns = {
        row["authority_name"]: (
            row["approved_count"],
            row["rejected_count"],
            row["decision_count"],
        )
        for row in report["authority_patterns"]
    }
    assert patterns == {
        "M7 Chief Warden": (2, 1, 3),
        "M7 Warden": (1, 1, 2),
    }

    completion = report["completion"]
    assert completion["completed_exchanges"] == 3
    assert completion["direct_exchanges"] == 2
    assert completion["multi_party_exchanges"] == 1
    assert completion["completed_vacant_transfers"] == 1
    assert completion["student_movements"] == 7
    assert completion["duration_sample_count"] == 6
    assert completion["duration_unavailable_count"] == 1
    assert completion["average_completion_time"] == timedelta(days=7, hours=12)


@pytest.mark.django_db
def test_reporting_scope_filters_dates_and_institutions(reporting_dataset):
    data = reporting_dataset
    report_b = build_exchange_report(
        user=data["dean"],
        window=data["window"],
        hostel_id=data["hostel_b"].id,
    )
    assert report_b["request_summary"]["total"] == 2
    assert report_b["match"]["matched_requests"] == 2
    assert report_b["match"]["rate"] == 100

    warden_report = build_exchange_report(user=data["warden"], window=data["window"])
    assert warden_report["request_summary"]["total"] == 6
    assert {row["hostel_name"] for row in warden_report["volume_by_hostel"]} == {
        "M7 North"
    }
    with pytest.raises(PermissionDenied):
        build_exchange_report(
            user=data["warden"],
            window=data["window"],
            hostel_id=data["hostel_b"].id,
        )

    empty_window = make_report_window(
        datetime(2026, 2, 1).date(),
        datetime(2026, 2, 2).date(),
    )
    empty = build_exchange_report(user=data["dean"], window=empty_window)
    assert empty["request_summary"]["total"] == 0
    assert empty["match"] == {
        "matched_requests": 0,
        "eligible_requests": 0,
        "rate": None,
    }
    assert empty["completion"]["average_completion_time"] is None


@pytest.mark.django_db
def test_reports_endpoint_authorization_filters_and_privacy(client, reporting_dataset):
    data = reporting_dataset
    reports_url = reverse("exchange:reports")

    for authorized_user in [data["dean"], data["chief"], data["warden"]]:
        client.force_login(authorized_user)
        response = client.get(
            reports_url,
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )
        assert response.status_code == 200
        assert b"Reports &amp; Analytics" in response.content
        assert b"HIDDEN STUDENT ALPHA" not in response.content
        assert response.headers["Server-Timing"].startswith("exchange-report;dur=")

    for denied_user in [data["admin"], data["sysadmin"], data["student"]]:
        client.force_login(denied_user)
        assert client.get(reports_url).status_code == 403

    client.force_login(data["warden"])
    invalid_hostel = client.get(
        reports_url,
        {
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "hostel": data["hostel_b"].id,
        },
    )
    assert invalid_hostel.status_code == 200
    assert b"Select a valid choice" in invalid_hostel.content

    invalid_dates = client.get(
        reports_url,
        {"start_date": "2026-01-31", "end_date": "2026-01-01"},
    )
    assert invalid_dates.status_code == 200
    assert b"End date must be on or after the start date" in invalid_dates.content

    client.force_login(data["admin"])
    assert client.get(reverse("exchange:policy_settings")).status_code == 200
    client.force_login(data["warden"])
    assert client.get(reverse("exchange:policy_settings")).status_code == 403


@pytest.mark.django_db
def test_audit_selector_is_scoped_and_report_query_budget_meets_ces(reporting_dataset):
    data = reporting_dataset
    audit_entries = list(
        get_exchange_audit_queryset(
            user=data["dean"],
            window=data["window"],
        )
    )
    assert audit_entries
    assert {entry.institution_id for entry in audit_entries} == {data["institution"].id}

    started = perf_counter()
    with CaptureQueriesContext(connection) as queries:
        report = build_exchange_report(user=data["dean"], window=data["window"])
    elapsed = perf_counter() - started
    assert report["request_summary"]["total"] == 8
    assert len(queries) <= 22
    assert elapsed < 3.0
    print(
        "M7_REPORT_PERFORMANCE "
        f"backend={connection.vendor} queries={len(queries)} wall_seconds={elapsed:.6f}"
    )


@pytest.mark.django_db
def test_representative_2000_request_report_response_meets_ces(
    client,
    reporting_dataset,
):
    data = reporting_dataset
    room = Room.objects.filter(floor__block__hostel=data["hostel_a"]).first()
    synthetic_users = User.objects.bulk_create(
        [
            User(
                institution=data["institution"],
                email=f"m7.report.load.{index}@example.test",
                full_name=f"M7 Report Load {index}",
                password="!",
                registration_number=f"M7-LOAD-{index:04d}",
                residential_eligibility=User.ResidentialEligibility.MALE,
            )
            for index in range(2000)
        ],
        batch_size=500,
    )
    synthetic_beds = Bed.objects.bulk_create(
        [
            Bed(
                room=room,
                bed_label=f"Load Bed {index:04d}",
                operational_status=OperationalStatus.IN_SERVICE,
            )
            for index in range(2000)
        ],
        batch_size=500,
    )
    synthetic_occupancies = StudentOccupancy.objects.bulk_create(
        [
            StudentOccupancy(
                institution=data["institution"],
                student=student,
                bed=bed,
                start_date=datetime(2025, 8, 1).date(),
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            )
            for student, bed in zip(
                synthetic_users,
                synthetic_beds,
                strict=True,
            )
        ],
        batch_size=500,
    )
    ExchangeRequest.objects.bulk_create(
        [
            ExchangeRequest(
                institution=data["institution"],
                student=student,
                current_occupancy=occupancy,
                request_type=ExchangeRequest.RequestType.EXCHANGE,
                reason="Deterministic M7 report response fixture",
                status=ExchangeRequest.Status.OPEN,
                submitted_at=_at(5),
            )
            for student, occupancy in zip(
                synthetic_users,
                synthetic_occupancies,
                strict=True,
            )
        ],
        batch_size=500,
    )

    client.force_login(data["dean"])
    started = perf_counter()
    with CaptureQueriesContext(connection) as queries:
        response = client.get(
            reverse("exchange:reports"),
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )
    elapsed = perf_counter() - started

    assert response.status_code == 200
    assert response.context["report"]["request_summary"]["total"] == 2008
    assert len(queries) <= 40
    assert elapsed < 3.0
    print(
        "M7_REPORT_RESPONSE_PERFORMANCE "
        f"backend={connection.vendor} requests=2008 queries={len(queries)} "
        f"wall_seconds={elapsed:.6f}"
    )
