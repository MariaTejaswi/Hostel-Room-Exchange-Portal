import uuid
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import Institution
from apps.exchange.models import ExchangeProposal, ProposalParticipant
from apps.exchange.services import execute_exchange_transfer


class Command(BaseCommand):
    help = "Atomically execute approved room exchange proposals (2-student swaps and 3-5 student rotations)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--proposal",
            type=str,
            required=False,
            help="UUID of a specific APPROVED ExchangeProposal to execute.",
        )
        parser.add_argument(
            "--institution",
            type=str,
            required=False,
            help="Institution code (e.g. APEX) or numeric ID to scope execution.",
        )
        parser.add_argument(
            "--actor",
            type=str,
            required=False,
            help="Username of the authorized authority executing this recovery (defaults to the approving authority).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate validation and permutation checks without modifying database state.",
        )

    def handle(self, *args, **options):
        proposal_arg = options.get("proposal")
        inst_arg = options.get("institution")
        actor_username = options.get("actor")
        dry_run = options.get("dry_run", False)

        actor_user = None
        if actor_username:
            User = get_user_model()
            try:
                actor_user = User.objects.get(username=actor_username)
            except User.DoesNotExist:
                raise CommandError(f"Actor user '{actor_username}' not found.")

        qs = ExchangeProposal.objects.filter(status=ExchangeProposal.Status.APPROVED)

        if inst_arg:
            try:
                if inst_arg.isdigit():
                    institution = Institution.objects.get(id=int(inst_arg))
                else:
                    institution = Institution.objects.get(code=inst_arg)
                qs = qs.filter(institution=institution)
            except Institution.DoesNotExist:
                raise CommandError(f"Institution '{inst_arg}' does not exist.")

        if proposal_arg:
            try:
                proposal_uuid = uuid.UUID(proposal_arg)
            except ValueError:
                raise CommandError(f"Invalid proposal UUID format: '{proposal_arg}'.")
            qs = qs.filter(id=proposal_uuid)

        proposals = list(qs.order_by("created_at"))
        if not proposals:
            self.stdout.write(self.style.WARNING("No pending approved exchange proposals found matching criteria."))
            return

        self.stdout.write(f"Found {len(proposals)} approved proposal(s) awaiting execution.")

        success_count = 0
        failure_count = 0

        for prop in proposals:
            self.stdout.write(f"\nProcessing Proposal #{str(prop.id)[:8]} ({prop.cycle_length}-student cycle)...")
            if dry_run:
                self.stdout.write(self.style.NOTICE(f"  [DRY-RUN] Validating cycle for Proposal #{str(prop.id)[:8]}..."))
                participants = list(ProposalParticipant.objects.filter(proposal=prop).select_related("student", "current_occupancy", "proposed_bed"))
                src_beds = [p.current_occupancy.bed_id for p in participants]
                dest_beds = [p.proposed_bed_id for p in participants]
                if set(src_beds) == set(dest_beds) and len(src_beds) == prop.cycle_length:
                    self.stdout.write(self.style.SUCCESS(f"  [DRY-RUN] Valid cycle permutation verified for {len(participants)} participants."))
                    success_count += 1
                else:
                    self.stdout.write(self.style.ERROR(f"  [DRY-RUN] Cycle permutation invalid: src={src_beds}, dest={dest_beds}"))
                    failure_count += 1
                continue

            try:
                records = execute_exchange_transfer(
                    proposal_id=prop.id,
                    actor=actor_user,
                    remarks="Executed via management recovery command.",
                    now=timezone.now(),
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Successfully executed Proposal #{str(prop.id)[:8]}: {len(records)} transfers completed."
                    )
                )
                for rec in records:
                    self.stdout.write(
                        f"    - {rec.student.full_name}: {rec.source_hostel_name} Rm {rec.source_room_number}-{rec.source_bed_label} -> "
                        f"{rec.destination_hostel_name} Rm {rec.destination_room_number}-{rec.destination_bed_label}"
                    )
                success_count += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  Failed to execute Proposal #{str(prop.id)[:8]}: {exc}"))
                failure_count += 1

        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(
            self.style.SUCCESS(f"Execution complete: {success_count} succeeded, {failure_count} failed.")
        )
