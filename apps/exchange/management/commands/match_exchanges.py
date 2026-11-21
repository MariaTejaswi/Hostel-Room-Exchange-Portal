import json
import platform
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from apps.core.models import Institution
from apps.exchange.matching import find_candidate_matches


class Command(BaseCommand):
    help = "Run the P04 RoomLoop Matching Engine to discover candidate room exchanges (2-way direct swaps and 3-5 person rotation cycles)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--institution",
            type=str,
            required=True,
            help="Institution code (e.g., APEX-TEST) or numeric ID.",
        )
        parser.add_argument(
            "--min-length",
            type=int,
            default=2,
            choices=[2, 3, 4, 5],
            help="Minimum cycle length to discover (default: 2).",
        )
        parser.add_argument(
            "--max-length",
            type=int,
            default=5,
            choices=[2, 3, 4, 5],
            help=(
                "Maximum cycle length requested by this run (default: 5). "
                "The institution policy remains authoritative."
            ),
        )
        parser.add_argument(
            "--max-cycles",
            type=int,
            default=10_000,
            help="Ceiling on detected cycles to avoid combinatorial explosion (default: 10,000).",
        )
        parser.add_argument(
            "--persist",
            action="store_true",
            help="Persist candidate matches as draft proposals (safe, non-locking, idempotent).",
        )
        parser.add_argument(
            "--format",
            type=str,
            default="table",
            choices=["table", "json"],
            help="Output format: table or json.",
        )

    def handle(self, *args, **options):
        inst_arg = options["institution"]
        try:
            if inst_arg.isdigit():
                institution = Institution.objects.get(id=int(inst_arg))
            else:
                institution = Institution.objects.get(code=inst_arg)
        except Institution.DoesNotExist:
            raise CommandError(f"Institution with code or ID '{inst_arg}' does not exist.")

        min_len = options["min_length"]
        max_len = options["max_length"]
        max_cycles = options["max_cycles"]
        persist = options["persist"]
        output_format = options["format"]

        # Run matching engine
        result = find_candidate_matches(
            institution=institution,
            min_length=min_len,
            max_length=max_len,
            max_cycles=max_cycles,
            persist_candidates=persist,
        )

        m = result.metrics

        if output_format == "json":
            json_data = {
                "institution": {
                    "id": institution.id,
                    "code": institution.code,
                    "name": institution.name,
                },
                "hardware": {
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "python_version": platform.python_version(),
                },
                "metrics": {
                    "graph_nodes": m.graph_nodes,
                    "graph_edges": m.graph_edges,
                    "total_candidates": m.total_candidates,
                    "candidates_by_length": m.candidates_by_length,
                    "same_hostel_count": m.same_hostel_count,
                    "cross_hostel_count": m.cross_hostel_count,
                    "limit_reached": m.limit_reached,
                    "timings": {
                        "data_loading_seconds": round(m.data_loading_time_seconds, 4),
                        "graph_building_seconds": round(m.graph_building_time_seconds, 4),
                        "cycle_detection_seconds": round(m.cycle_detection_time_seconds, 4),
                        "total_seconds": round(m.total_time_seconds, 4),
                    },
                },
                "proposals_persisted": len(result.proposals_created),
                "candidates": [
                    {
                        "cycle_length": c.cycle_length,
                        "classification": c.classification,
                        "cycle_signature": c.cycle_signature,
                        "detected_at": c.detected_at.isoformat(),
                        "participants": [
                            {
                                "request_id": p.request_id,
                                "student_id": str(p.student_id),
                                "registration": p.student_registration,
                                "current_hostel": p.current_hostel_name,
                                "current_room": p.current_room_number,
                                "current_bed": p.current_bed_label,
                                "proposed_hostel": p.proposed_hostel_name,
                                "proposed_room": p.proposed_room_number,
                                "proposed_bed": p.proposed_bed_label,
                                "explanation": p.preference_match_explanation,
                            }
                            for p in c.participants
                        ],
                    }
                    for c in result.candidates
                ],
            }
            self.stdout.write(json.dumps(json_data, indent=2, cls=DjangoJSONEncoder))
            return

        # Table / Text output
        self.stdout.write("=" * 72)
        self.stdout.write(f"ROOMLOOP MATCHING ENGINE (P04-M3)")
        self.stdout.write(f"Institution: {institution.name} ({institution.code})")
        self.stdout.write(f"Platform: {platform.system()} {platform.machine()} | Python {platform.python_version()}")
        self.stdout.write("=" * 72)

        self.stdout.write(self.style.MIGRATE_HEADING("\n[1] Graph Construction & Request Eligibility"))
        self.stdout.write(f"  • Open Eligible Requests (Graph Nodes) : {m.graph_nodes}")
        self.stdout.write(f"  • Compatible Transitions (Graph Edges) : {m.graph_edges}")

        self.stdout.write(self.style.MIGRATE_HEADING("\n[2] Candidate Match Results"))
        self.stdout.write(f"  • Total Candidate Matches Found        : {m.total_candidates}")
        for k in sorted(m.candidates_by_length.keys()):
            self.stdout.write(f"    - {k}-way cycles                     : {m.candidates_by_length[k]}")
        self.stdout.write(f"  • Same-Hostel Exchanges                : {m.same_hostel_count}")
        self.stdout.write(f"  • Cross-Hostel Exchanges               : {m.cross_hostel_count}")
        if m.limit_reached:
            self.stdout.write(self.style.WARNING(f"  ! Cycle limit reached ({max_cycles}). Search was bounded."))
        else:
            self.stdout.write("  • Complete Exhaustive Search           : Yes (no limit reached)")

        self.stdout.write(self.style.MIGRATE_HEADING("\n[3] Performance & Timing Breakdown"))
        self.stdout.write(f"  • Data Loading & Pre-filtering         : {m.data_loading_time_seconds:.4f}s")
        self.stdout.write(f"  • Graph Indexing & Edge Construction   : {m.graph_building_time_seconds:.4f}s")
        self.stdout.write(f"  • Cycle Traversal & Canonicalization   : {m.cycle_detection_time_seconds:.4f}s")
        self.stdout.write(f"  • Total Elapsed Time                   : {m.total_time_seconds:.4f}s")

        self.stdout.write(self.style.MIGRATE_HEADING("\n[4] Persistence & Invariants"))
        if persist:
            self.stdout.write(self.style.SUCCESS(f"  • Persisted Draft Proposals            : {len(result.proposals_created)} created"))
            self.stdout.write("  • Request Status & Occupancy Invariance: All requests preserved OPEN; zero bed changes.")
        else:
            self.stdout.write("  • Mode                                 : Discovery Only (Dry Run)")
            self.stdout.write("  • Requests & Occupancies Unaltered     : Yes (zero mutations)")
            self.stdout.write("  (Pass --persist to safely record draft candidate proposals without locking requests)")

        if result.candidates:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n[5] Detected Candidate Matches (First {min(10, len(result.candidates))} of {len(result.candidates)}):"))
            for idx, c in enumerate(result.candidates[:10], start=1):
                self.stdout.write(f"\n  Candidate #{idx}: {c.cycle_length}-way {c.classification} [Signature: {c.cycle_signature}]")
                for p_idx, p in enumerate(c.participants, start=1):
                    self.stdout.write(
                        f"    Step {p_idx}: Student {p.student_registration or p.student_id} "
                        f"({p.current_hostel_name} R{p.current_room_number}:{p.current_bed_label}) "
                        f"-> Proposed: ({p.proposed_hostel_name} R{p.proposed_room_number}:{p.proposed_bed_label})"
                    )
                    self.stdout.write(f"            Reason: {p.preference_match_explanation}")

        self.stdout.write("\n" + "=" * 72)
        self.stdout.write("Notice: Candidate detection is discovery only. It is not student consent,")
        self.stdout.write("warden approval, or transfer execution. Proposals must be explicitly")
        self.stdout.write("accepted and approved before atomic occupancy transfers.")
        self.stdout.write("=" * 72 + "\n")
