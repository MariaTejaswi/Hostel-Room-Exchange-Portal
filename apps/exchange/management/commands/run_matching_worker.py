from django.core.management.base import BaseCommand, CommandError
from apps.core.models import Institution
from apps.exchange.services import run_institution_matching


class Command(BaseCommand):
    help = "Run background matching worker pass for an institution or all institutions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--institution",
            type=str,
            help="Institution code or primary key ID.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Run matching across all active institutions sequentially.",
        )

    def handle(self, *args, **options):
        inst_arg = options.get("institution")
        run_all = options.get("all")

        if not inst_arg and not run_all:
            raise CommandError("Please specify either --institution <code> or --all.")

        institutions = []
        if run_all:
            institutions = list(Institution.objects.filter(is_active=True))
        else:
            if inst_arg.isdigit():
                institutions = list(Institution.objects.filter(id=int(inst_arg)))
            else:
                institutions = list(Institution.objects.filter(code__iexact=inst_arg))

            if not institutions:
                raise CommandError(f"No institution found matching '{inst_arg}'.")

        for inst in institutions:
            self.stdout.write(f"Starting matching run for {inst.name} ({inst.code})...")
            try:
                job = run_institution_matching(
                    institution_id=inst.id,
                    trigger_reason="cli_run_matching_worker",
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Matching completed for {inst.code}: Status={job.status}, "
                        f"Candidates found={job.candidates_found_count}, "
                        f"Proposals activated={job.proposals_activated_count}."
                    )
                )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"Matching failed for {inst.code}: {exc}"))
