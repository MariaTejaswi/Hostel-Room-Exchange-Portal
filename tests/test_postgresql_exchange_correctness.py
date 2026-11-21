"""PostgreSQL transaction, locking, and transfer-reconciliation evidence.

Run this module only with ``config.settings.test_postgresql``.  The settings
module and ``setUp`` both fail closed unless the active database is an isolated
PostgreSQL database whose name starts with ``test_``.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier
from typing import ClassVar
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import (
    ROLE_STUDENT,
    ROLE_WARDEN,
    create_user_with_role,
    ensure_roles_exist,
)
from apps.core.models import Institution
from apps.exchange.models import (
    Approval,
    ExchangeProposal,
    ExchangeRequest,
    PolicyRule,
    ProposalParticipant,
    TransferRecord,
)
from apps.exchange.services import (
    approve_and_execute_exchange_proposal,
    decide_exchange_proposal,
    execute_exchange_transfer,
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
from apps.hostels.services import assign_student_to_bed


@dataclass
class ThreadOutcome:
    label: str
    backend_pid: int | None
    database_name: str | None
    value: object = None
    error: BaseException | None = None


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock evidence only")
class PostgreSQLExchangeCorrectnessTests(TransactionTestCase):
    """Exercise real row locks using committed setup data and independent connections."""

    databases: ClassVar[set[str]] = {"default"}

    def setUp(self):
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            current_database = cursor.fetchone()[0]

        configured_test_database = connection.settings_dict["TEST"]["NAME"]
        self.assertEqual(connection.vendor, "postgresql")
        self.assertEqual(current_database, configured_test_database)
        self.assertTrue(current_database.lower().startswith("test_"))

        self.institution = Institution.objects.create(
            code="PG-CORRECTNESS",
            name="PostgreSQL Correctness University",
            contact_email="pg-correctness@example.test",
            is_active=True,
        )
        ensure_roles_exist()
        self.warden = create_user_with_role(
            email="pg-warden@example.test",
            full_name="PostgreSQL Test Warden",
            role_name=ROLE_WARDEN,
            institution=self.institution,
            password="TestPassword123!",
        )
        self.hostel = Hostel.objects.create(
            institution=self.institution,
            name="PostgreSQL Test Hostel",
            code="PG-TH",
            resident_category=Hostel.ResidentCategory.MALE,
            status=Hostel.Status.ACTIVE,
        )
        self.block = Block.objects.create(hostel=self.hostel, name="Block A", code="A")
        self.floor = Floor.objects.create(
            block=self.block,
            floor_number=1,
            floor_label="First Floor",
        )
        WardenAssignment.objects.create(
            warden=self.warden,
            hostel=self.hostel,
            is_primary=True,
        )
        self._configure_policies()
        self._sequence = 0

    def _configure_policies(self):
        values = {
            PolicyRule.Key.COOLING_OFF_DAYS: 0,
            PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
            PolicyRule.Key.MAX_CYCLE_LENGTH: 5,
            PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
            PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
            PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS: 48,
        }
        for key, value in values.items():
            PolicyRule.objects.create(
                institution=self.institution,
                key=key,
                value={"value": value},
                is_active=True,
            )

    def _create_residents(self, count, *, prefix):
        students = []
        beds = []
        occupancies = []
        for index in range(count):
            self._sequence += 1
            student = create_user_with_role(
                email=f"{prefix}-{self._sequence}@example.test",
                full_name=f"{prefix.title()} Student {index + 1}",
                role_name=ROLE_STUDENT,
                institution=self.institution,
                password="TestPassword123!",
                registration_number=f"PG-{prefix.upper()}-{self._sequence:04d}",
                residential_eligibility=User.ResidentialEligibility.MALE,
            )
            room = Room.objects.create(
                floor=self.floor,
                room_number=f"PG-{self._sequence:04d}",
                room_type=Room.RoomType.SINGLE,
                occupancy_category="Standard",
                operational_status=OperationalStatus.IN_SERVICE,
            )
            bed = Bed.objects.create(
                room=room,
                bed_label="Bed A",
                operational_status=OperationalStatus.IN_SERVICE,
            )
            occupancy = assign_student_to_bed(
                student=student,
                bed=bed,
                created_by=self.warden,
            )
            students.append(student)
            beds.append(bed)
            occupancies.append(occupancy)
        return students, beds, occupancies

    def _create_pending_proposal(
        self,
        *,
        students,
        occupancies,
        destination_beds,
        prefix,
    ):
        now = timezone.now()
        requests = []
        for index, (student, occupancy, destination_bed) in enumerate(
            zip(students, occupancies, destination_beds, strict=True)
        ):
            request = ExchangeRequest.objects.create(
                institution=self.institution,
                student=student,
                current_occupancy=occupancy,
                preferred_bed=destination_bed,
                preferred_hostel=self.hostel,
                reason=f"{prefix} movement {index + 1}",
                status=ExchangeRequest.Status.MATCHED,
                submitted_at=now,
                expires_at=now + timedelta(days=30),
            )
            requests.append(request)

        proposal = ExchangeProposal.objects.create(
            institution=self.institution,
            status=ExchangeProposal.Status.PENDING_APPROVAL,
            cycle_length=len(students),
            cycle_signature=f"pg:{prefix}:{self._sequence}:{now.timestamp()}",
            expires_at=now + timedelta(hours=48),
            matched_at=now,
            accepted_at=now,
            created_by=self.warden,
        )
        participants = []
        for index, (student, occupancy, destination_bed, request) in enumerate(
            zip(students, occupancies, destination_beds, requests, strict=True)
        ):
            participants.append(
                ProposalParticipant.objects.create(
                    proposal=proposal,
                    exchange_request=request,
                    student=student,
                    current_occupancy=occupancy,
                    proposed_bed=destination_bed,
                    sequence_order=index,
                    response_status=ProposalParticipant.ResponseStatus.ACCEPTED,
                    responded_at=now,
                    is_active_reservation=True,
                )
            )
        return {
            "proposal": proposal,
            "participants": participants,
            "requests": requests,
            "students": students,
            "occupancies": occupancies,
            "destination_beds": destination_beds,
        }

    def _build_cycle(self, count, *, prefix):
        students, beds, occupancies = self._create_residents(count, prefix=prefix)
        destination_beds = beds[1:] + beds[:1]
        fixture = self._create_pending_proposal(
            students=students,
            occupancies=occupancies,
            destination_beds=destination_beds,
            prefix=prefix,
        )
        fixture["beds"] = beds
        return fixture

    def _approve_and_execute_operation(self, proposal_id):
        def operation():
            actor = User.objects.get(pk=self.warden.pk)
            return approve_and_execute_exchange_proposal(
                proposal_id=proposal_id,
                actor=actor,
                remarks="PostgreSQL concurrency evidence",
            )

        return operation

    def _run_concurrently(self, operations, *, timeout=45):
        """Start operations together on distinct PostgreSQL backend connections."""
        barrier = Barrier(len(operations), timeout=10)

        def worker(label, operation):
            close_old_connections()
            backend_pid = None
            database_name = None
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SET statement_timeout = '30s'")
                    cursor.execute("SELECT pg_backend_pid(), current_database()")
                    backend_pid, database_name = cursor.fetchone()
                barrier.wait(timeout=10)
                return ThreadOutcome(
                    label=label,
                    backend_pid=backend_pid,
                    database_name=database_name,
                    value=operation(),
                )
            except Exception as error:  # noqa: BLE001 - return worker failure to test thread
                return ThreadOutcome(
                    label=label,
                    backend_pid=backend_pid,
                    database_name=database_name,
                    error=error,
                )
            finally:
                connections.close_all()

        executor = ThreadPoolExecutor(max_workers=len(operations))
        futures = [
            executor.submit(worker, label, operation)
            for label, operation in operations.items()
        ]
        try:
            outcomes = [future.result(timeout=timeout) for future in futures]
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        backend_pids = [outcome.backend_pid for outcome in outcomes]
        self.assertNotIn(None, backend_pids)
        self.assertEqual(len(backend_pids), len(set(backend_pids)))
        expected_database = connection.settings_dict["TEST"]["NAME"]
        self.assertEqual(
            {outcome.database_name for outcome in outcomes},
            {expected_database},
        )
        return {outcome.label: outcome for outcome in outcomes}

    def _assert_active_mapping(self, students, expected_beds):
        active = StudentOccupancy.objects.filter(
            student_id__in=[student.id for student in students],
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        self.assertEqual(active.count(), len(students))
        self.assertEqual(active.values("student_id").distinct().count(), len(students))
        self.assertEqual(active.values("bed_id").distinct().count(), len(students))
        actual = dict(active.values_list("student_id", "bed_id"))
        self.assertEqual(actual, expected_beds)

    def test_overlapping_destination_approvals_cannot_partially_rotate(self):
        valid = self._build_cycle(2, prefix="overlap-valid")
        competing_students, _, competing_occupancies = self._create_residents(
            2,
            prefix="overlap-competing",
        )
        competing = self._create_pending_proposal(
            students=competing_students,
            occupancies=competing_occupancies,
            destination_beds=valid["beds"],
            prefix="overlap-competing",
        )

        outcomes = self._run_concurrently(
            {
                "valid": self._approve_and_execute_operation(valid["proposal"].id),
                "competing": self._approve_and_execute_operation(
                    competing["proposal"].id
                ),
            }
        )

        self.assertIsNone(outcomes["valid"].error)
        self.assertIsInstance(outcomes["competing"].error, ValidationError)
        self.assertIn("closed cycle permutation", str(outcomes["competing"].error))

        valid["proposal"].refresh_from_db()
        competing["proposal"].refresh_from_db()
        self.assertEqual(valid["proposal"].status, ExchangeProposal.Status.COMPLETED)
        self.assertEqual(
            competing["proposal"].status,
            ExchangeProposal.Status.PENDING_APPROVAL,
        )
        self.assertEqual(TransferRecord.objects.filter(proposal=valid["proposal"]).count(), 2)
        self.assertFalse(
            TransferRecord.objects.filter(proposal=competing["proposal"]).exists()
        )
        self.assertFalse(Approval.objects.filter(proposal=competing["proposal"]).exists())

        self._assert_active_mapping(
            valid["students"],
            {
                valid["students"][0].id: valid["beds"][1].id,
                valid["students"][1].id: valid["beds"][0].id,
            },
        )
        self._assert_active_mapping(
            competing_students,
            {
                student.id: occupancy.bed_id
                for student, occupancy in zip(
                    competing_students,
                    competing_occupancies,
                    strict=True,
                )
            },
        )
        self.assertEqual(
            ProposalParticipant.objects.filter(
                proposal=competing["proposal"],
                is_active_reservation=True,
            ).count(),
            2,
        )

    def test_duplicate_concurrent_approval_and_execution_is_idempotent(self):
        fixture = self._build_cycle(3, prefix="duplicate")
        proposal_id = fixture["proposal"].id

        outcomes = self._run_concurrently(
            {
                "first": self._approve_and_execute_operation(proposal_id),
                "second": self._approve_and_execute_operation(proposal_id),
            }
        )

        self.assertIsNone(outcomes["first"].error)
        self.assertIsNone(outcomes["second"].error)
        record_sets = [
            {record.id for record in outcome.value[2]}
            for outcome in outcomes.values()
        ]
        self.assertEqual(record_sets[0], record_sets[1])
        self.assertEqual(len(record_sets[0]), 3)

        fixture["proposal"].refresh_from_db()
        self.assertEqual(fixture["proposal"].status, ExchangeProposal.Status.COMPLETED)
        self.assertEqual(Approval.objects.filter(proposal_id=proposal_id).count(), 1)
        self.assertEqual(TransferRecord.objects.filter(proposal_id=proposal_id).count(), 3)
        self.assertEqual(
            StudentOccupancy.objects.filter(
                student_id__in=[student.id for student in fixture["students"]],
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).count(),
            3,
        )
        self.assertFalse(
            ProposalParticipant.objects.filter(
                proposal_id=proposal_id,
                is_active_reservation=True,
            ).exists()
        )

    def test_independent_exchanges_complete_on_independent_connections(self):
        direct = self._build_cycle(2, prefix="independent-direct")
        rotation = self._build_cycle(4, prefix="independent-rotation")

        outcomes = self._run_concurrently(
            {
                "direct": self._approve_and_execute_operation(direct["proposal"].id),
                "rotation": self._approve_and_execute_operation(
                    rotation["proposal"].id
                ),
            }
        )

        for outcome in outcomes.values():
            self.assertIsNone(outcome.error)
        self.assertEqual(
            ExchangeProposal.objects.filter(
                id__in=[direct["proposal"].id, rotation["proposal"].id],
                status=ExchangeProposal.Status.COMPLETED,
            ).count(),
            2,
        )
        self.assertEqual(
            TransferRecord.objects.filter(
                proposal_id__in=[direct["proposal"].id, rotation["proposal"].id]
            ).count(),
            6,
        )
        active = StudentOccupancy.objects.filter(
            student_id__in=[
                student.id
                for student in direct["students"] + rotation["students"]
            ],
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        self.assertEqual(active.count(), 6)
        self.assertEqual(active.values("bed_id").distinct().count(), 6)

    def test_one_hundred_exchanges_reconcile_after_every_execution(self):
        direct_students, _, _ = self._create_residents(2, prefix="sim-direct")
        rotation_students, _, _ = self._create_residents(3, prefix="sim-rotation")
        all_students = direct_students + rotation_students
        movement_counts = {student.id: 0 for student in all_students}

        completed_exchanges = 0
        individual_movements = 0
        for index in range(100):
            students = direct_students if index % 2 == 0 else rotation_students
            occupancies_by_student = {
                occupancy.student_id: occupancy
                for occupancy in StudentOccupancy.objects.filter(
                    student_id__in=[student.id for student in students],
                    status=StudentOccupancy.OccupancyStatus.ACTIVE,
                ).select_related("bed")
            }
            occupancies = [occupancies_by_student[student.id] for student in students]
            current_beds = [occupancy.bed for occupancy in occupancies]
            destination_beds = current_beds[1:] + current_beds[:1]
            fixture = self._create_pending_proposal(
                students=students,
                occupancies=occupancies,
                destination_beds=destination_beds,
                prefix=f"simulation-{index + 1:03d}",
            )

            _, proposal, records = approve_and_execute_exchange_proposal(
                proposal_id=fixture["proposal"].id,
                actor=self.warden,
                remarks=f"Simulation exchange {index + 1}",
            )
            completed_exchanges += 1
            individual_movements += len(students)
            for student in students:
                movement_counts[student.id] += 1

            self.assertEqual(proposal.status, ExchangeProposal.Status.COMPLETED)
            self.assertEqual(len(records), len(students))
            expected_beds = {
                student.id: destination_bed.id
                for student, destination_bed in zip(
                    students,
                    destination_beds,
                    strict=True,
                )
            }
            self._assert_active_mapping(students, expected_beds)
            self._assert_simulation_reconciled(
                all_students=all_students,
                movement_counts=movement_counts,
                completed_exchanges=completed_exchanges,
                individual_movements=individual_movements,
            )

        self.assertEqual(completed_exchanges, 100)
        self.assertEqual(individual_movements, 250)
        self.assertEqual(
            ExchangeProposal.objects.filter(status=ExchangeProposal.Status.COMPLETED).count(),
            100,
        )
        self.assertEqual(TransferRecord.objects.count(), 250)

    def _assert_simulation_reconciled(
        self,
        *,
        all_students,
        movement_counts,
        completed_exchanges,
        individual_movements,
    ):
        student_ids = [student.id for student in all_students]
        active = StudentOccupancy.objects.filter(
            student_id__in=student_ids,
            status=StudentOccupancy.OccupancyStatus.ACTIVE,
        )
        self.assertEqual(active.count(), len(all_students))
        self.assertEqual(active.values("student_id").distinct().count(), len(all_students))
        self.assertEqual(active.values("bed_id").distinct().count(), len(all_students))
        self.assertEqual(
            StudentOccupancy.objects.filter(
                student_id__in=student_ids,
                status=StudentOccupancy.OccupancyStatus.TRANSFERRED,
            ).count(),
            individual_movements,
        )
        self.assertEqual(
            StudentOccupancy.objects.filter(student_id__in=student_ids).count(),
            len(all_students) + individual_movements,
        )
        self.assertEqual(TransferRecord.objects.count(), individual_movements)
        self.assertEqual(
            ExchangeProposal.objects.filter(status=ExchangeProposal.Status.COMPLETED).count(),
            completed_exchanges,
        )
        self.assertEqual(
            ExchangeRequest.objects.filter(status=ExchangeRequest.Status.COMPLETED).count(),
            individual_movements,
        )
        self.assertFalse(
            ProposalParticipant.objects.filter(is_active_reservation=True).exists()
        )

        for student in all_students:
            expected_movements = movement_counts[student.id]
            self.assertEqual(
                TransferRecord.objects.filter(student=student).count(),
                expected_movements,
            )
            self.assertEqual(
                StudentOccupancy.objects.filter(student=student).count(),
                expected_movements + 1,
            )

        for record in TransferRecord.objects.select_related(
            "participant",
            "previous_occupancy",
            "new_occupancy",
        ):
            self.assertEqual(record.participant.student_id, record.student_id)
            self.assertEqual(record.previous_occupancy_id, record.participant.current_occupancy_id)
            self.assertEqual(record.previous_bed_id, record.previous_occupancy.bed_id)
            self.assertEqual(record.new_bed_id, record.new_occupancy.bed_id)
            self.assertEqual(record.new_bed_id, record.participant.proposed_bed_id)

    def test_mid_execution_failure_rolls_back_and_retry_is_idempotent(self):
        fixture = self._build_cycle(3, prefix="rollback")
        proposal_id = fixture["proposal"].id
        decide_exchange_proposal(
            proposal_id=proposal_id,
            actor=self.warden,
            decision="approve",
            remarks="Approve before injected execution failure",
        )

        original_create = TransferRecord.objects.create
        create_calls = 0

        def fail_on_second_transfer_record(*args, **kwargs):
            nonlocal create_calls
            create_calls += 1
            if create_calls == 2:
                raise RuntimeError("injected mid-execution transfer-record failure")
            return original_create(*args, **kwargs)

        with patch.object(
            TransferRecord.objects,
            "create",
            side_effect=fail_on_second_transfer_record,
        ), self.assertRaisesRegex(RuntimeError, "injected mid-execution"):
            execute_exchange_transfer(
                proposal_id=proposal_id,
                actor=self.warden,
                remarks="Expected rollback",
            )

        fixture["proposal"].refresh_from_db()
        self.assertEqual(fixture["proposal"].status, ExchangeProposal.Status.APPROVED)
        self.assertFalse(TransferRecord.objects.filter(proposal_id=proposal_id).exists())
        self.assertEqual(
            StudentOccupancy.objects.filter(
                id__in=[occupancy.id for occupancy in fixture["occupancies"]],
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).count(),
            3,
        )
        self.assertEqual(
            StudentOccupancy.objects.filter(
                student_id__in=[student.id for student in fixture["students"]]
            ).count(),
            3,
        )
        self.assertEqual(
            ProposalParticipant.objects.filter(
                proposal_id=proposal_id,
                is_active_reservation=True,
            ).count(),
            3,
        )
        self.assertEqual(
            ExchangeRequest.objects.filter(
                id__in=[request.id for request in fixture["requests"]],
                status=ExchangeRequest.Status.MATCHED,
            ).count(),
            3,
        )

        first_records = execute_exchange_transfer(
            proposal_id=proposal_id,
            actor=self.warden,
            remarks="Successful retry",
        )
        repeated_records = execute_exchange_transfer(
            proposal_id=proposal_id,
            actor=self.warden,
            remarks="Idempotent replay",
        )

        self.assertEqual(len(first_records), 3)
        self.assertEqual(
            {record.id for record in first_records},
            {record.id for record in repeated_records},
        )
        self.assertEqual(TransferRecord.objects.filter(proposal_id=proposal_id).count(), 3)
        self.assertEqual(
            StudentOccupancy.objects.filter(
                student_id__in=[student.id for student in fixture["students"]],
                status=StudentOccupancy.OccupancyStatus.ACTIVE,
            ).count(),
            3,
        )
