import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("procurement", "0007_alter_bill_id_alter_billline_id_and_more"),
        ("accounts", "0008_alter_company_id_alter_companysubscription_id_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="notes",
            field=models.TextField(
                blank=True, help_text="Internal notes; not sent to the vendor."
            ),
        ),
        migrations.CreateModel(
            name="VendorEmail",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("to_email", models.EmailField(blank=True, max_length=254)),
                ("cc", models.CharField(blank=True, help_text="Comma-separated", max_length=500)),
                ("bcc", models.CharField(blank=True, help_text="Comma-separated", max_length=500)),
                ("subject", models.CharField(blank=True, max_length=300)),
                ("body_html", models.TextField(blank=True)),
                ("body_edited", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("queued", "Queued"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True)),
                (
                    "company",
                    models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+", to="accounts.company",
                    ),
                ),
                (
                    "vendor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="emails", to="procurement.vendor",
                    ),
                ),
                (
                    "purchase_orders",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Every order this message covers.",
                        related_name="emails",
                        to="procurement.purchaseorder",
                    ),
                ),
                (
                    "sent_by",
                    models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="VendorEmailAttachment",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to="vendor_emails/")),
                ("filename", models.CharField(max_length=255)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "email",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments", to="procurement.vendoremail",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="vendoremail",
            index=models.Index(
                fields=["company", "status"], name="proc_email_scope_idx"
            ),
        ),
    ]
