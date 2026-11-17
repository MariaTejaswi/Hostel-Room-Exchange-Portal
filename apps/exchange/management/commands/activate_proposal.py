import uuid
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Institution
from apps.exchange.models import ExchangeProposal
from apps.exchange.services import activate_exchange_proposal


class Command(BaseCommand):
    help = "Activate a draft candidate match into an active ExchangeProposal awaiting participant acceptance."

    def add_arguments(self, parser):
        parser.add_argument(
            "--proposal",
            type=str,
            required=True,
            help="UUID of the draft ExchangeProposal to activate.",
        )
        parser.add_argument(
            "--institution",
            type=str,
            required=False,
            help="Optional institution code (e.g., APEX) or numeric ID to scope and verify.",
        )

    def handle(self, *args, **options):
        proposal_arg = options["proposal"]
        try:
            proposal_uuid = uuid.UUID(proposal_arg)
        except ValueError:
            raise CommandError(f"Invalid proposal UUID format: '{proposal_arg}'.")

        proposal_qs = ExchangeProposal.objects.filter(id=proposal_uuid)

        inst_arg = options.get("institution")
        if inst_arg:
            try:
                if inst_arg.isdigit():
                    institution = Institution.objects.get(id=int(inst_arg))
                else:
                    institution = Institution.objects.get(code=inst_arg)
                proposal_qs = proposal_qs.filter(institution=institution)
            except Institution.DoesNotExist:
                raise CommandError(f"Institution '{inst_arg}' does not exist.")

        proposal = proposal_qs.first()
        if not proposal:
            raise CommandError(f"ExchangeProposal with ID '{proposal_uuid}' not found.")

        try:
            activated_proposal = activate_exchange_proposal(
                proposal_id=proposal.id,
                acting_user=None,
            )
        except Exception as exc:
            raise CommandError(f"Failed to activate proposal {proposal_uuid}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully activated ExchangeProposal {activated_proposal.id}."
            )
        )
        self.stdout.write(f"Institution: {activated_proposal.institution.name} ({activated_proposal.institution.code})")
        self.stdout.write(f"Status: {activated_proposal.get_status_display()}")
        self.stdout.write(f"Cycle Length: {activated_proposal.cycle_length}")
        self.stdout.write(
            f"Acceptance Deadline: {activated_proposal.expires_at.strftime('%Y-%m-%d %H:%M:%S %Z') if activated_proposal.expires_at else 'None'}"
        )

        participants = activated_proposal.participants.select_related(
            "student",
            "current_occupancy__bed__room__floor__block__hostel",
            "proposed_bed__room__floor__block__hostel",
        ).order_by("sequence_order")

        self.stdout.write("\nParticipants in Cycle:")
        for p in participants:
            curr = f"{p.current_occupancy.bed.room.floor.block.hostel.name} Rm {p.current_occupancy.bed.room.room_number} Bed {p.current_occupancy.bed.bed_label}"
            prop = f"{p.proposed_bed.room.floor.block.hostel.name} Rm {p.proposed_bed.room.room_number} Bed {p.proposed_bed.bed_label}"
            self.stdout.write(
                f"  [{p.sequence_order + 1}] Student {p.student.full_name}: Current [{curr}] -> Proposed [{prop}] (Status: {p.response_status})"
            )
