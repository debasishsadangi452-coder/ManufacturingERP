"""Scheduled QuickBooks sync for every connected company.

Intended to run once every 24 hours from an external scheduler (Heroku
Scheduler, cron, Windows Task Scheduler):

    python manage.py sync_quickbooks

Pull is the reason this job exists: the post_save signals in signals.py already
mirror ERP writes into QuickBooks, but edits made *in QuickBooks* never reach
the ERP until someone syncs. The push_all backfill afterwards is self-healing --
it retries records whose auto-push failed while QuickBooks was unreachable.

Each connection is isolated: sync_master_data re-raises so one company's expired
token cannot abort the run for every other tenant.
"""

from django.core.management.base import BaseCommand, CommandError

from quickbooks.models import QuickBooksConnection
from quickbooks.push import push_all
from quickbooks.services import QuickBooksAPIError


class Command(BaseCommand):
    help = "Pull QuickBooks data and backfill pending pushes for all connected companies."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            dest="company_slug",
            default="",
            help="Only sync this company slug (default: every active connection).",
        )
        parser.add_argument(
            "--sync-type",
            dest="sync_type",
            default="all",
            help="Pull scope passed to sync_master_data (default: all).",
        )
        parser.add_argument(
            "--no-push",
            action="store_true",
            help="Skip the push backfill and only pull from QuickBooks.",
        )

    def handle(self, *args, **options):
        # Imported here so --help works even if QuickBooks config is absent.
        from quickbooks.services import sync_master_data

        connections = QuickBooksConnection.objects.filter(is_active=True).select_related("company")
        if options["company_slug"]:
            connections = connections.filter(company__slug=options["company_slug"])

        if not connections.exists():
            self.stdout.write("No active QuickBooks connections; nothing to sync.")
            return

        failures = 0
        for connection in connections:
            name = connection.company.name
            try:
                run = sync_master_data(connection, sync_type=options["sync_type"])
                self.stdout.write(
                    f"{name}: pulled created={run.records_created} "
                    f"updated={run.records_updated} seen={run.records_seen}"
                )
            except (QuickBooksAPIError, ValueError) as exc:
                # sync_master_data already recorded a failed run + sync error row.
                failures += 1
                self.stderr.write(f"{name}: pull failed: {exc}")
                continue

            if options["no_push"]:
                continue

            # push_all never raises -- individual failures land in
            # QuickBooksSyncError and mark the run failed.
            run = push_all(connection)
            if run.status == "failed":
                failures += 1
                self.stderr.write(f"{name}: push backfill finished with errors; see sync errors.")
            else:
                self.stdout.write(
                    f"{name}: pushed created={run.records_created} seen={run.records_seen}"
                )

        if failures:
            # Exit non-zero so a scheduler (Railway cron, cron, Task Scheduler)
            # reports the run as failed instead of silently succeeding while
            # tenants go unsynced. Companies that did sync are unaffected.
            raise CommandError(
                f"Sync finished with {failures} of {len(connections)} connection(s) failing; "
                "see QuickBooksSyncError rows."
            )
        self.stdout.write(self.style.SUCCESS("Sync complete."))
