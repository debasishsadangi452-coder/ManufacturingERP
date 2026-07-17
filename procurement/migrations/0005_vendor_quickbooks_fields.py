from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0004_vendor_company_alter_goodsreceipt_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="outstanding_balance",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="vendor",
            name="payment_terms",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="vendor",
            name="quickbooks_id",
            field=models.CharField(blank=True, db_index=True, max_length=100),
        ),
        migrations.AddField(
            model_name="vendor",
            name="quickbooks_last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="vendor",
            name="quickbooks_sync_token",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="vendor",
            name="tax_id",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
