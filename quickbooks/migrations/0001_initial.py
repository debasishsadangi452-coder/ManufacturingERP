import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("accounts", "0006_alter_user_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="QuickBooksConnection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("realm_id", models.CharField(db_index=True, max_length=100)),
                ("environment", models.CharField(choices=[("sandbox", "Sandbox"), ("production", "Production")], default="sandbox", max_length=20)),
                ("access_token_signed", models.TextField(blank=True)),
                ("refresh_token_signed", models.TextField(blank=True)),
                ("access_token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("refresh_token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("company_name", models.CharField(blank=True, max_length=255)),
                ("connected_at", models.DateTimeField(auto_now_add=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("company", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="quickbooks_connection", to="accounts.company")),
                ("connected_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-connected_at"]},
        ),
        migrations.CreateModel(
            name="QuickBooksEntityLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entity_type", models.CharField(choices=[("customer", "Customer"), ("vendor", "Vendor"), ("item", "Item"), ("invoice", "Invoice"), ("bill", "Bill"), ("payment", "Payment"), ("purchase_order", "Purchase Order"), ("report", "Report")], max_length=50)),
                ("local_object_id", models.PositiveIntegerField()),
                ("quickbooks_id", models.CharField(db_index=True, max_length=100)),
                ("sync_token", models.CharField(blank=True, max_length=100)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="accounts.company")),
            ],
            options={"unique_together": {("company", "entity_type", "quickbooks_id")}},
        ),
        migrations.CreateModel(
            name="QuickBooksSyncRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sync_type", models.CharField(max_length=50)),
                ("status", models.CharField(choices=[("running", "Running"), ("success", "Success"), ("failed", "Failed")], default="running", max_length=20)),
                ("records_created", models.IntegerField(default=0)),
                ("records_updated", models.IntegerField(default=0)),
                ("records_seen", models.IntegerField(default=0)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="accounts.company")),
                ("connection", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sync_runs", to="quickbooks.quickbooksconnection")),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.CreateModel(
            name="QuickBooksSyncError",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entity_type", models.CharField(blank=True, max_length=50)),
                ("quickbooks_id", models.CharField(blank=True, max_length=100)),
                ("message", models.TextField()),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="accounts.company")),
                ("sync_run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="errors", to="quickbooks.quickbookssyncrun")),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
