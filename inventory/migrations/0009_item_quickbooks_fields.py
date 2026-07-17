from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0008_item_selling_price"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="purchase_cost",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="item",
            name="quickbooks_id",
            field=models.CharField(blank=True, db_index=True, max_length=100),
        ),
        migrations.AddField(
            model_name="item",
            name="quickbooks_last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="quickbooks_sync_token",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="item",
            name="reorder_point",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="sku",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
