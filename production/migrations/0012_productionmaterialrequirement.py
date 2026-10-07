# Generated manually for Track B material consumption integrity.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0021_intermediate_items"),
        ("production", "0011_recipe_default_line"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductionMaterialRequirement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("required_quantity", models.FloatField(default=0)),
                ("reserved_quantity", models.FloatField(default=0)),
                ("issued_quantity", models.FloatField(default=0)),
                ("consumed_quantity", models.FloatField(default=0)),
                ("shortage_quantity", models.FloatField(default=0)),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="inventory.item")),
                (
                    "production_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="material_requirements",
                        to="production.productionorder",
                    ),
                ),
            ],
            options={
                "unique_together": {("production_order", "item")},
            },
        ),
    ]
