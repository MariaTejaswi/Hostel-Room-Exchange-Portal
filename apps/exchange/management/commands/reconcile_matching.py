from django.core.management.base import BaseCommand
from apps.exchange.services import reconcile_all_institutions_matching


class Command(BaseCommand):
    help = "Run periodic background matching reconciliation sweep across all institutions."

    def handle(self, *args, **options):
        self.stdout.write("Running exchange matching reconciliation sweep...")
        scheduled_inst_ids = reconcile_all_institutions_matching()
        if scheduled_inst_ids:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Reconciliation completed: scheduled matching for {len(scheduled_inst_ids)} "
                    f"institution(s) (IDs: {scheduled_inst_ids})."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("Reconciliation completed: all institutions up to date. No matching jobs required.")
            )
