import django.db.models.deletion
from django.db import migrations, models


def link_existing_requests(apps, schema_editor):
    """Best-effort link for requests raised before the FK existed.

    A procuring request is matched to a purchase order that contains the same
    item and was created after the request. Only used when exactly one
    candidate exists — an ambiguous match is left null rather than guessed,
    since a wrong link would notify the wrong production order.
    """
    InventoryRequest = apps.get_model("inventory", "InventoryRequest")
    PurchaseOrderItem = apps.get_model("procurement", "PurchaseOrderItem")

    for req in InventoryRequest.objects.filter(status="procuring", purchase_order__isnull=True):
        candidates = list(
            PurchaseOrderItem.objects
            .filter(item_id=req.item_id, purchase_order__created_at__gte=req.created_at)
            .values_list("purchase_order_id", flat=True)
            .distinct()[:2]
        )
        if len(candidates) == 1:
            req.purchase_order_id = candidates[0]
            req.save(update_fields=["purchase_order"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0018_cyclecount_stocktransfer_cyclecountline"),
        ("procurement", "0008_vendor_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventoryrequest",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inventory_requests",
                to="procurement.purchaseorder",
            ),
        ),
        migrations.RunPython(link_existing_requests, migrations.RunPython.noop),
    ]
