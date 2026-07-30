"""Close inventory requests whose production order has already finished.

An InventoryRequest is raised when a production order is short of a material.
Until now nothing closed them again, so once the batch was produced the request
stayed "pending" indefinitely — the store's queue filled with shortages that no
longer needed acting on, and clicking Procure on one produced a confusing
"no vendor price list" error for material that was never actually needed.

The signal now cancels these at completion time; this command clears the
backlog that accumulated before that fix.

Dry-run by default:

    python manage.py close_stale_requests --company 26
    python manage.py close_stale_requests --company 26 --apply
    python manage.py close_stale_requests --all --apply
"""

from django.core.management.base import BaseCommand, CommandError

from inventory.models import InventoryRequest


class Command(BaseCommand):
    help = "Cancel pending inventory requests whose production order is already completed."

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Restrict to one company id.")
        parser.add_argument("--all", action="store_true", help="Apply across every company.")
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")

    def handle(self, *args, **options):
        company_id = options["company"]
        every = options["all"]
        apply_changes = options["apply"]

        if not company_id and not every:
            raise CommandError("Pass --company <id> or --all.")

        stale = InventoryRequest.objects.filter(
            status__in=["pending", "procuring"],
            production_order__status="completed",
        ).select_related("item", "production_order", "item__company")

        if company_id:
            stale = stale.filter(item__company_id=company_id)

        rows = list(stale.order_by("item__company_id", "id"))
        if not rows:
            self.stdout.write(self.style.SUCCESS("No stale requests found."))
            return

        current = None
        for r in rows:
            company = r.item.company
            if company != current:
                current = company
                self.stdout.write(f"\n{company}:")
            self.stdout.write(
                f"  #{r.id:<5} {r.item.name:20} qty={r.quantity:>14,.0f}  "
                f"PO#{r.production_order_id} ({r.production_order.status})"
            )

        self.stdout.write("")
        if apply_changes:
            count = InventoryRequest.objects.filter(id__in=[r.id for r in rows]).update(status="cancelled")
            self.stdout.write(self.style.SUCCESS(f"Cancelled {count} stale request(s)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"{len(rows)} stale request(s) — dry run, nothing written. Re-run with --apply."
            ))
