# Generated manually for Track B agreed order price snapshots.

from django.db import migrations, models


def backfill_unit_price(apps, schema_editor):
    SalesOrderItem = apps.get_model("sales", "SalesOrderItem")
    for line in SalesOrderItem.objects.select_related("item").iterator():
        line.unit_price = line.item.selling_price or 0
        line.save(update_fields=["unit_price"])


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0012_alter_customer_address_alter_customer_email_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesorderitem",
            name="unit_price",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.RunPython(backfill_unit_price, migrations.RunPython.noop),
    ]
