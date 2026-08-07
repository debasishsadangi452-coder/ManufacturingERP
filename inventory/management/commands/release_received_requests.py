"""Release inventory requests whose material already arrived.

Goods receipt did not previously close out the request that triggered the
procurement, so requests raised before that fix can sit in "procuring" forever
even though the stock landed. This finds them and releases them, notifying
production exactly as a live receipt now does.

A request is only released when its material is genuinely on hand — either its
linked PO is received, or (for requests with no link) every candidate PO
carrying that item is received. Anything still outstanding is left alone.
"""

from django.core.management.base import BaseCommand

from inventory.models import InventoryRequest, Stock
from procurement.models import PurchaseOrderItem

OPEN_STATUSES = ("pending", "procuring")


class Command(BaseCommand):
    help = "Release inventory requests whose procured material has already been received."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be released without writing anything.",
        )
        parser.add_argument(
            "--notify", action="store_true",
            help="Also send production the 'material received' notification.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        notify = options["notify"]

        requests = (
            InventoryRequest.objects
            .filter(status__in=OPEN_STATUSES)
            .select_related("item", "warehouse", "production_order", "purchase_order")
            .order_by("id")
        )

        released = 0
        skipped_outstanding = 0
        skipped_no_stock = 0

        for req in requests:
            po = req.purchase_order
            if po is not None:
                if po.status != "received":
                    skipped_outstanding += 1
                    continue
            else:
                # No link (raised before the FK existed). Only safe to release
                # if every PO that could have covered this item is received.
                candidates = set(
                    PurchaseOrderItem.objects
                    .filter(item_id=req.item_id, purchase_order__created_at__gte=req.created_at)
                    .values_list("purchase_order__status", flat=True)
                )
                if not candidates or candidates != {"received"}:
                    skipped_outstanding += 1
                    continue

            # Only release if the stock is actually sitting in the warehouse —
            # a received PO whose goods were already consumed should not be
            # reported to production as available.
            stock = Stock.objects.filter(item=req.item, warehouse=req.warehouse).first()
            on_hand = stock.quantity if stock else 0
            if on_hand <= 0:
                skipped_no_stock += 1
                self.stdout.write(
                    f"  req#{req.id} {req.item.name}: PO received but no stock on hand — left open"
                )
                continue

            prod = req.production_order
            label = f" (Production Order #{prod.id})" if prod else ""
            self.stdout.write(
                f"  req#{req.id} {req.quantity:g} {req.item.unit} {req.item.name}"
                f"{label} → supplied"
            )

            if dry:
                released += 1
                continue

            # The InventoryRequest post_save signal turns this transition into
            # the "material received" notification for production, so saving is
            # all that is needed — notifying here as well would double up.
            if notify:
                req.status = "supplied"
                req.save(update_fields=["status"])
            else:
                # Release quietly: skip the signal so back-filling old records
                # does not flood production with alerts about stale deliveries.
                InventoryRequest.objects.filter(pk=req.pk).update(status="supplied")
            released += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{'Would release' if dry else 'Released'}: {released}"
        ))
        self.stdout.write(
            f"Left open — PO still outstanding: {skipped_outstanding}, "
            f"no stock on hand: {skipped_no_stock}"
        )
        if released and not notify and not dry:
            self.stdout.write(
                "Pass --notify to also alert production about these releases."
            )
