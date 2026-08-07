from django.db import migrations, models


# Existing rows predate the `module` column, so they all default to "general"
# and would vanish from every page filter. Classify them by the related_type
# they were filed against, falling back to the role that received them.
RELATED_TYPE_TO_MODULE = {
    "ProductionOrder": "production",
    "QualityCheck": "quality",
    "InventoryRequest": "inventory",
    "Item": "inventory",
    "Stock": "inventory",
    "GoodsReceipt": "procurement",
    "PurchaseOrder": "procurement",
    "SalesOrder": "sales",
    "Invoice": "sales",
    "ExpenseRequest": "finance",
    "MaintenanceTask": "maintenance",
    "Equipment": "maintenance",
}

ROLE_TO_MODULE = {
    "production": "production",
    "store": "inventory",
    "sales": "sales",
    "quality": "quality",
    "finance": "finance",
    "hr": "workforce",
}


def backfill_module(apps, schema_editor):
    Notification = apps.get_model("core", "Notification")

    for related_type, module in RELATED_TYPE_TO_MODULE.items():
        Notification.objects.filter(related_type=related_type).update(module=module)

    # Anything still unclassified falls back to the recipient's home page.
    # Admin rows have no natural home, so they stay "general".
    for role, module in ROLE_TO_MODULE.items():
        Notification.objects.filter(
            module="general", recipient_role=role
        ).update(module=module)


def unbackfill(apps, schema_editor):
    """No-op: reversing the schema change drops the column anyway."""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_notification_company"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="module",
            field=models.CharField(
                choices=[
                    ("production", "Production"),
                    ("inventory", "Inventory"),
                    ("sales", "Sales"),
                    ("quality", "Quality"),
                    ("procurement", "Procurement"),
                    ("finance", "Finance"),
                    ("maintenance", "Maintenance"),
                    ("logistics", "Logistics"),
                    ("workforce", "Workforce"),
                    ("general", "General"),
                ],
                db_index=True,
                default="general",
                help_text="Which page this notification belongs to.",
                max_length=30,
            ),
        ),
        migrations.AlterModelOptions(
            name="notification",
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["company", "recipient_role", "module"],
                name="core_notif_scope_idx",
            ),
        ),
        migrations.RunPython(backfill_module, unbackfill),
    ]
