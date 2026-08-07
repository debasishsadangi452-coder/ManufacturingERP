"""Draft vendor emails for orders placed before the email feature existed.

Drafting is normally tied to the ordering action, so orders placed earlier have
no email. This backfills them, grouping each vendor's placed orders into one
email — the same rule a live "Order All" follows.

Only orders that were actually placed are considered. A PO still in draft,
pending or approved has not been committed to the vendor and gets no email.

Safe to re-run: orders already covered by an email are skipped.
"""

from django.core.management.base import BaseCommand

from procurement.emails import draft_for_orders
from procurement.models import PurchaseOrder, VendorEmail

# A PO reaches the vendor once it is placed. "received" is included because
# those orders were necessarily placed first.
PLACED_STATUSES = ("ordered", "received")


class Command(BaseCommand):
    help = "Create draft vendor emails for previously-placed purchase orders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be drafted without writing anything.",
        )
        parser.add_argument(
            "--reset", action="store_true",
            help=(
                "Delete existing unsent, unedited drafts first. Use after changing "
                "the grouping rule so old groupings are rebuilt. Never touches sent "
                "or hand-edited drafts."
            ),
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]

        if options["reset"]:
            stale = VendorEmail.objects.filter(status="draft", body_edited=False)
            count = stale.count()
            protected = VendorEmail.objects.exclude(
                status="draft", body_edited=False
            ).count()
            if dry:
                self.stdout.write(f"Would delete {count} unsent, unedited draft(s).")
            else:
                stale.delete()
                self.stdout.write(f"Deleted {count} unsent, unedited draft(s).")
            if protected:
                self.stdout.write(
                    f"Kept {protected} sent or hand-edited email(s) untouched."
                )
            self.stdout.write("")

        orders = (
            PurchaseOrder.objects
            .filter(status__in=PLACED_STATUSES)
            .select_related("vendor")
            .prefetch_related("items__item", "emails")
            .order_by("id")
        )

        eligible, no_email, already, no_items = [], [], 0, 0
        for po in orders:
            if po.emails.exists():
                already += 1
                continue
            if not po.items.exists():
                no_items += 1
                continue
            if not po.vendor or not po.vendor.email:
                no_email.append(po)
                continue
            eligible.append(po)

        # Group for reporting exactly as draft_for_orders will group for real.
        grouped = {}
        for po in eligible:
            grouped.setdefault(po.vendor.name, []).append(po)

        for vendor_name, pos in sorted(grouped.items()):
            numbers = ", ".join(f"PO-{p.id:04d}" for p in pos)
            verb = "would draft" if dry else "drafting"
            self.stdout.write(f"  {vendor_name}: {verb} 1 email covering {numbers}")

        created = [] if dry else draft_for_orders(eligible)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{'Would create' if dry else 'Created'}: "
            f"{len(grouped) if dry else len(created)} email(s) "
            f"covering {len(eligible)} placed order(s)"
        ))
        self.stdout.write(
            f"Skipped — already emailed: {already}, "
            f"no line items: {no_items}, "
            f"vendor has no email: {len(no_email)}"
        )
        for po in no_email:
            self.stdout.write(
                f"    PO-{po.id:04d}  vendor '{po.vendor.name}' has no email address"
            )
        if no_email and not dry:
            self.stdout.write(self.style.WARNING(
                "Add addresses to those vendors, then re-run to draft them."
            ))
