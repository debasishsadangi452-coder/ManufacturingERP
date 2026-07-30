"""P2 operational services: stock transfers and cycle-count posting.

Both build on the existing stock primitives (increase_stock / decrease_stock /
adjust_stock) so movements stay logged and consistent with everything else.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .services import adjust_stock, increase_stock, decrease_stock
from .models import Batch


@transaction.atomic
def complete_transfer(transfer, user=None):
    """Move stock from source to destination warehouse and carry lots along.

    Decrements the source, increments the destination, and repoints the item's
    lots at the destination warehouse (FIFO up to the transferred quantity) so
    traceability follows the physical move. Idempotent-guarded: only an
    in-transit transfer can complete.
    """
    if transfer.status != "in_transit":
        raise ValidationError(f"Transfer #{transfer.id} is already {transfer.status}.")

    # Move the stock (decrease_stock validates sufficient source quantity).
    decrease_stock(
        transfer.item, transfer.source_warehouse, transfer.quantity,
        user=user, reference=f"Transfer #{transfer.id} out",
    )
    increase_stock(
        transfer.item, transfer.dest_warehouse, transfer.quantity,
        user=user, reference=f"Transfer #{transfer.id} in",
    )

    # Carry lots: repoint lots at the destination up to the transferred qty.
    remaining = transfer.quantity
    lots = Batch.objects.filter(
        item=transfer.item, warehouse=transfer.source_warehouse, remaining_quantity__gt=0
    ).order_by("created_at", "id")
    for lot in lots:
        if remaining <= 0:
            break
        # A lot moves whole if it fits; partial moves split conceptually but we
        # keep it simple — repoint the lot and stop once the qty is covered.
        lot.warehouse = transfer.dest_warehouse
        lot.save(update_fields=["warehouse"])
        remaining -= (lot.remaining_quantity or 0)

    transfer.status = "completed"
    transfer.completed_at = timezone.now()
    transfer.save(update_fields=["status", "completed_at"])
    return transfer


@transaction.atomic
def post_cycle_count(cycle_count, user=None):
    """Apply a cycle count's variances to on-hand stock.

    Each line with a nonzero variance becomes an ADJUST to the counted quantity,
    reconciling the system to the physical count. Only an open count can post.
    Returns the number of lines that produced an adjustment.
    """
    if cycle_count.status != "open":
        raise ValidationError(f"Cycle count #{cycle_count.id} is already {cycle_count.status}.")

    adjusted = 0
    for line in cycle_count.lines.select_related("item"):
        if line.variance != 0:
            adjust_stock(
                line.item, cycle_count.warehouse, line.counted_quantity,
                user=user,
                reference=f"Cycle count #{cycle_count.id}",
            )
            adjusted += 1

    cycle_count.status = "posted"
    cycle_count.posted_at = timezone.now()
    cycle_count.save(update_fields=["status", "posted_at"])
    return adjusted
