"""Reproducible PostgreSQL benchmark for the production RoomLoop P04 matcher."""

import cProfile
import io
import json
import os
import platform
import pstats
from dataclasses import asdict
from datetime import timedelta
from statistics import median
from time import perf_counter
from typing import ClassVar
from unittest import skipUnless

import django
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import Institution
from apps.exchange.models import ExchangeProposal, ExchangeRequest, PolicyRule
from apps.exchange.services import run_exchange_matching
from apps.hostels.models import (
    Bed,
    Block,
    Floor,
    Hostel,
    OperationalStatus,
    Room,
    StudentOccupancy,
)

STRUCTURED_EXPECTED = {2: 400, 3: 80, 4: 40, 5: 20}
STRUCTURED_MATCHED_REQUESTS = sum(
    cycle_length * count for cycle_length, count in STRUCTURED_EXPECTED.items()
)
REQUEST_COUNT = 2_000
REPEAT_COUNT = int(os.getenv("P04_BENCH_RUNS", "6"))
MAX_CYCLES = 10_000


@skipUnless(connection.vendor == "postgresql", "PostgreSQL benchmark only")
class TestPostgreSQLMatchingPerformance(TransactionTestCase):
    databases: ClassVar = {"default"}

    def test_two_thousand_open_request_scenarios(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            database_name, postgres_version = cursor.fetchone()
        self.assertTrue(database_name.lower().startswith("test_"))
        self.assertEqual(database_name, connection.settings_dict["TEST"]["NAME"])

        fixture_start = perf_counter()
        structured = self._create_structured_dataset()
        print("STRUCTURED_FIXTURE_READY", flush=True)
        broad = self._create_broad_dataset()
        print("BROAD_FIXTURE_READY", flush=True)
        fixture_seconds = perf_counter() - fixture_start
        print(f"FIXTURE_SECONDS={fixture_seconds:.6f}", flush=True)

        self.assertEqual(
            ExchangeRequest.objects.filter(
                institution=structured,
                status=ExchangeRequest.Status.OPEN,
            ).count(),
            REQUEST_COUNT,
        )
        self.assertEqual(
            ExchangeRequest.objects.filter(
                institution=broad,
                status=ExchangeRequest.Status.OPEN,
            ).count(),
            REQUEST_COUNT,
        )

        initial_occupancy_count = StudentOccupancy.objects.count()
        initial_occupancy_map = dict(
            StudentOccupancy.objects.values_list("student_id", "bed_id")
        )

        if os.getenv("P04_BENCH_MODE", "detection") == "persistence":
            persistence = self._measure_persistence(structured)
            self.assertEqual(
                persistence["created_proposals"], sum(STRUCTURED_EXPECTED.values())
            )
            self.assertEqual(StudentOccupancy.objects.count(), initial_occupancy_count)
            self.assertEqual(
                dict(StudentOccupancy.objects.values_list("student_id", "bed_id")),
                initial_occupancy_map,
            )
            return

        structured_runs = [
            self._measure_detection(structured, "structured", run_index)
            for run_index in range(1, REPEAT_COUNT + 1)
        ]
        broad_runs = [
            self._measure_detection(broad, "broad", run_index)
            for run_index in range(1, REPEAT_COUNT + 1)
        ]

        for run in structured_runs:
            self.assertEqual(run["graph_nodes"], REQUEST_COUNT)
            self.assertEqual(run["graph_edges"], STRUCTURED_MATCHED_REQUESTS)
            self.assertEqual(run["candidate_count"], sum(STRUCTURED_EXPECTED.values()))
            self.assertEqual(run["candidates_by_length"], STRUCTURED_EXPECTED)
            self.assertFalse(run["limit_reached"])
            self.assertLess(run["service_matching_seconds"], 5.0)

        for run in broad_runs:
            self.assertEqual(run["graph_nodes"], REQUEST_COUNT)
            self.assertEqual(run["graph_edges"], 20 * 100 * 99)
            self.assertEqual(run["candidate_count"], MAX_CYCLES)
            self.assertTrue(run["limit_reached"])
            if os.getenv("P04_BENCH_PROFILE", "0") != "1":
                self.assertLess(run["service_matching_seconds"], 5.0)

        persistence = None
        if os.getenv("P04_BENCH_SKIP_PERSIST", "0") != "1":
            persistence = self._measure_persistence(structured)
            self.assertEqual(
                persistence["created_proposals"], sum(STRUCTURED_EXPECTED.values())
            )
            self.assertEqual(
                ExchangeProposal.objects.filter(institution=structured).count(),
                sum(STRUCTURED_EXPECTED.values()),
            )
            self.assertFalse(
                ExchangeProposal.objects.filter(
                    institution=structured,
                    participants__is_active_reservation=True,
                ).exists()
            )

        self.assertEqual(StudentOccupancy.objects.count(), initial_occupancy_count)
        self.assertEqual(
            dict(StudentOccupancy.objects.values_list("student_id", "bed_id")),
            initial_occupancy_map,
        )
        self.assertFalse(
            StudentOccupancy.objects.exclude(
                status=StudentOccupancy.OccupancyStatus.ACTIVE
            ).exists()
        )
        self.assertEqual(
            ExchangeRequest.objects.filter(status=ExchangeRequest.Status.OPEN).count(),
            REQUEST_COUNT * 2,
        )

        ExchangeProposal.objects.filter(institution=structured).delete()

        report = {
            "runtime": {
                "database_backend": connection.vendor,
                "database_name": database_name,
                "postgres": postgres_version,
                "python": platform.python_version(),
                "django": django.get_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "logical_cpu_count": os.cpu_count(),
            },
            "fixture_creation_seconds_excluded": fixture_seconds,
            "dataset": {
                "structured": {
                    "open_requests": REQUEST_COUNT,
                    "direct_cycles": 400,
                    "three_person_cycles": 80,
                    "four_person_cycles": 40,
                    "five_person_cycles": 20,
                    "unmatched_requests": REQUEST_COUNT - STRUCTURED_MATCHED_REQUESTS,
                    "expected_cycles": sum(STRUCTURED_EXPECTED.values()),
                    "expected_edges": STRUCTURED_MATCHED_REQUESTS,
                },
                "broad": {
                    "open_requests": REQUEST_COUNT,
                    "hostel_cohorts": 20,
                    "requests_per_cohort": 100,
                    "expected_edges": 20 * 100 * 99,
                    "production_candidate_limit": MAX_CYCLES,
                },
            },
            "structured_runs": structured_runs,
            "broad_runs": broad_runs,
            "summaries": {
                "structured": self._summary(structured_runs),
                "broad": self._summary(broad_runs),
            },
            "structured_persistence": persistence,
        }
        print("\nP04_BENCHMARK_JSON_START")
        print(json.dumps(report, indent=2, sort_keys=True))
        print("P04_BENCHMARK_JSON_END")

    def _create_institution(self, *, code, name):
        institution = Institution.objects.create(
            code=code,
            name=name,
            contact_email=f"{code.lower()}@example.test",
            is_active=True,
        )
        policies = {
            PolicyRule.Key.COOLING_OFF_DAYS: 0,
            PolicyRule.Key.REQUEST_VALIDITY_DAYS: 30,
            PolicyRule.Key.MAX_CYCLE_LENGTH: 5,
            PolicyRule.Key.REQUIRE_FEE_CLEARANCE: False,
            PolicyRule.Key.REQUIRE_NO_DISCIPLINARY_HOLD: False,
            PolicyRule.Key.PROPOSAL_ACCEPTANCE_WINDOW_HOURS: 48,
        }
        PolicyRule.objects.bulk_create(
            [
                PolicyRule(
                    institution=institution,
                    key=key,
                    value={"value": value},
                    is_active=True,
                )
                for key, value in policies.items()
            ]
        )
        return institution

    def _create_inventory(self, *, institution, prefix, hostel_sizes):
        hostels = Hostel.objects.bulk_create(
            [
                Hostel(
                    institution=institution,
                    name=f"{prefix} Hostel {index + 1}",
                    code=f"{prefix[:3].upper()}-{index + 1:02d}",
                    resident_category=Hostel.ResidentCategory.MALE,
                    status=Hostel.Status.ACTIVE,
                )
                for index in range(len(hostel_sizes))
            ]
        )
        blocks = Block.objects.bulk_create(
            [
                Block(hostel=hostel, name="Block A", code="A", is_active=True)
                for hostel in hostels
            ]
        )
        floors = Floor.objects.bulk_create(
            [
                Floor(
                    block=block,
                    floor_number=1,
                    floor_label="First Floor",
                    is_active=True,
                )
                for block in blocks
            ]
        )

        rooms = []
        hostel_index_by_room = []
        global_index = 0
        for hostel_index, (floor, size) in enumerate(
            zip(floors, hostel_sizes, strict=True)
        ):
            for local_index in range(size):
                global_index += 1
                rooms.append(
                    Room(
                        floor=floor,
                        room_number=f"{local_index + 1:04d}",
                        room_type=Room.RoomType.DOUBLE,
                        occupancy_category="Standard",
                        accessibility_attributes="Near elevator",
                        operational_status=OperationalStatus.IN_SERVICE,
                        is_active=True,
                    )
                )
                hostel_index_by_room.append(hostel_index)
        rooms = Room.objects.bulk_create(rooms, batch_size=500)
        beds = Bed.objects.bulk_create(
            [
                Bed(
                    room=room,
                    bed_label="Bed A",
                    operational_status=OperationalStatus.IN_SERVICE,
                    is_active=True,
                )
                for room in rooms
            ],
            batch_size=500,
        )
        return hostels, beds, hostel_index_by_room

    def _create_students_and_occupancies(self, *, institution, beds, prefix):
        users = User.objects.bulk_create(
            [
                User(
                    email=f"{prefix}-{index:04d}@example.test",
                    full_name=f"{prefix.title()} Student {index:04d}",
                    institution=institution,
                    registration_number=f"{prefix.upper()}-{index:04d}",
                    residential_eligibility=User.ResidentialEligibility.MALE,
                    account_status=User.AccountStatus.ACTIVE,
                    is_active=True,
                    password="!",
                )
                for index in range(len(beds))
            ],
            batch_size=500,
        )
        start_date = timezone.now().date() - timedelta(days=365)
        occupancies = StudentOccupancy.objects.bulk_create(
            [
                StudentOccupancy(
                    institution=institution,
                    student=user,
                    bed=bed,
                    start_date=start_date,
                    status=StudentOccupancy.OccupancyStatus.ACTIVE,
                    source=StudentOccupancy.AllocationSource.SEED,
                )
                for user, bed in zip(users, beds, strict=True)
            ],
            batch_size=500,
        )
        return users, occupancies

    def _create_structured_dataset(self):
        institution = self._create_institution(
            code="BENCH-STRUCTURED",
            name="Structured Matching Benchmark",
        )
        hostels, beds, _ = self._create_inventory(
            institution=institution,
            prefix="structured",
            hostel_sizes=[REQUEST_COUNT],
        )
        users, occupancies = self._create_students_and_occupancies(
            institution=institution,
            beds=beds,
            prefix="structured",
        )

        destinations = [None] * REQUEST_COUNT
        cursor = 0
        for cycle_length, cycle_count in STRUCTURED_EXPECTED.items():
            for _ in range(cycle_count):
                indexes = list(range(cursor, cursor + cycle_length))
                for offset, source_index in enumerate(indexes):
                    destinations[source_index] = beds[
                        indexes[(offset + 1) % cycle_length]
                    ]
                cursor += cycle_length

        now = timezone.now()
        requests = []
        for index, (user, occupancy) in enumerate(zip(users, occupancies, strict=True)):
            values = {
                "preferred_hostel": hostels[0],
                "preferred_room_type": "",
                "preferred_occupancy_category": "",
                "preferred_accessibility_attributes": "",
            }
            if destinations[index] is not None:
                values["preferred_bed"] = destinations[index]
            else:
                unmatched_offset = index - STRUCTURED_MATCHED_REQUESTS
                if unmatched_offset < 250:
                    values["preferred_room_type"] = Room.RoomType.SINGLE
                elif unmatched_offset < 475:
                    values["preferred_occupancy_category"] = "Quiet Wing"
                else:
                    values["preferred_accessibility_attributes"] = (
                        "Wheelchair accessible"
                    )
            requests.append(
                ExchangeRequest(
                    institution=institution,
                    student=user,
                    current_occupancy=occupancy,
                    request_type=ExchangeRequest.RequestType.EXCHANGE,
                    reason="Deterministic structured benchmark request",
                    status=ExchangeRequest.Status.OPEN,
                    matching_status=ExchangeRequest.MatchingStatus.WAITING,
                    submitted_at=now,
                    expires_at=now + timedelta(days=30),
                    **values,
                )
            )
        ExchangeRequest.objects.bulk_create(requests, batch_size=500)
        return institution

    def _create_broad_dataset(self):
        institution = self._create_institution(
            code="BENCH-BROAD",
            name="Broad Preference Benchmark",
        )
        hostels, beds, hostel_indexes = self._create_inventory(
            institution=institution,
            prefix="broad",
            hostel_sizes=[100] * 20,
        )
        users, occupancies = self._create_students_and_occupancies(
            institution=institution,
            beds=beds,
            prefix="broad",
        )
        now = timezone.now()
        ExchangeRequest.objects.bulk_create(
            [
                ExchangeRequest(
                    institution=institution,
                    student=user,
                    current_occupancy=occupancy,
                    request_type=ExchangeRequest.RequestType.EXCHANGE,
                    preferred_hostel=hostels[hostel_indexes[index]],
                    preferred_room_type=Room.RoomType.DOUBLE,
                    preferred_occupancy_category="Standard",
                    reason="Broad hostel, type, and category preference benchmark",
                    status=ExchangeRequest.Status.OPEN,
                    matching_status=ExchangeRequest.MatchingStatus.WAITING,
                    submitted_at=now,
                    expires_at=now + timedelta(days=30),
                )
                for index, (user, occupancy) in enumerate(
                    zip(users, occupancies, strict=True)
                )
            ],
            batch_size=500,
        )
        return institution

    def _measure_detection(self, institution, scenario, run_index):
        self.assertFalse(
            ExchangeProposal.objects.filter(institution=institution).exists()
        )
        profiler = None
        if os.getenv("P04_BENCH_PROFILE", "0") == "1" and scenario == "broad":
            profiler = cProfile.Profile()
        with CaptureQueriesContext(connection) as queries:
            wall_start = perf_counter()
            if profiler is None:
                result = run_exchange_matching(
                    institution=institution,
                    max_length=5,
                    max_cycles=MAX_CYCLES,
                    persist_candidates=False,
                )
            else:
                result = profiler.runcall(
                    run_exchange_matching,
                    institution=institution,
                    max_length=5,
                    max_cycles=MAX_CYCLES,
                    persist_candidates=False,
                )
            wall_seconds = perf_counter() - wall_start

        if profiler is not None:
            profile_output = io.StringIO()
            pstats.Stats(profiler, stream=profile_output).strip_dirs().sort_stats(
                "cumulative"
            ).print_stats(35)
            print("BROAD_PROFILE_START", flush=True)
            print(profile_output.getvalue(), flush=True)
            print("BROAD_PROFILE_END", flush=True)

        signatures = [candidate.cycle_signature for candidate in result.candidates]
        self.assertEqual(len(signatures), len(set(signatures)))
        for candidate in result.candidates:
            self.assertLessEqual(candidate.cycle_length, 5)
            self.assertEqual(
                len(candidate.request_ids), len(set(candidate.request_ids))
            )
            student_ids = [
                participant.student_id for participant in candidate.participants
            ]
            current_beds = [
                participant.current_bed_id for participant in candidate.participants
            ]
            proposed_beds = [
                participant.proposed_bed_id for participant in candidate.participants
            ]
            self.assertEqual(len(student_ids), len(set(student_ids)))
            self.assertEqual(len(current_beds), len(set(current_beds)))
            self.assertEqual(set(current_beds), set(proposed_beds))

        metrics = asdict(result.metrics)
        measured = {
            "scenario": scenario,
            "run": run_index,
            "wall_seconds": wall_seconds,
            "query_count": len(queries),
            "graph_nodes": metrics["graph_nodes"],
            "graph_edges": metrics["graph_edges"],
            "candidate_count": metrics["total_candidates"],
            "candidates_by_length": {
                int(key): value
                for key, value in metrics["candidates_by_length"].items()
            },
            "limit_reached": metrics["limit_reached"],
            "policy_resolution_seconds": metrics["policy_resolution_time_seconds"],
            "database_loading_seconds": metrics["data_loading_time_seconds"],
            "eligibility_evaluation_seconds": metrics[
                "eligibility_evaluation_time_seconds"
            ],
            "graph_construction_seconds": metrics["graph_building_time_seconds"],
            "cycle_detection_seconds": metrics["cycle_detection_time_seconds"],
            "candidate_materialization_seconds": metrics[
                "candidate_materialization_time_seconds"
            ],
            "explanation_generation_seconds": metrics[
                "explanation_generation_time_seconds"
            ],
            "service_matching_seconds": metrics["total_time_seconds"],
        }
        print("MATCH_RUN=" + json.dumps(measured, sort_keys=True), flush=True)
        return measured

    def _measure_persistence(self, institution):
        self.assertFalse(
            ExchangeProposal.objects.filter(institution=institution).exists()
        )
        with CaptureQueriesContext(connection) as queries:
            wall_start = perf_counter()
            result = run_exchange_matching(
                institution=institution,
                max_length=5,
                max_cycles=MAX_CYCLES,
                persist_candidates=True,
            )
            wall_seconds = perf_counter() - wall_start
        measured = {
            "wall_seconds": wall_seconds,
            "matching_seconds_excludes_persistence": result.metrics.total_time_seconds,
            "persistence_seconds": result.metrics.persistence_time_seconds,
            "service_total_with_persistence_seconds": (
                result.metrics.total_with_persistence_time_seconds
            ),
            "query_count": len(queries),
            "created_proposals": len(result.proposals_created),
            "created_participants": sum(
                proposal.cycle_length for proposal in result.proposals_created
            ),
        }
        print("PERSISTENCE_RUN=" + json.dumps(measured, sort_keys=True), flush=True)
        return measured

    def _summary(self, runs):
        durations = [run["service_matching_seconds"] for run in runs]
        walls = [run["wall_seconds"] for run in runs]
        return {
            "service_durations": durations,
            "service_median_seconds": median(durations),
            "service_slowest_seconds": max(durations),
            "wall_median_seconds": median(walls),
            "wall_slowest_seconds": max(walls),
            "query_counts": [run["query_count"] for run in runs],
            "all_under_five_seconds": all(duration < 5.0 for duration in durations),
        }
