from django.db import migrations


def seed_units(apps, schema_editor):
    from inventory.uom import EXTRA_UNITS

    UnitOfMeasure = apps.get_model("inventory", "UnitOfMeasure")
    for code, name, dimension, to_base, is_base in EXTRA_UNITS:
        UnitOfMeasure.objects.get_or_create(
            company=None,
            code=code,
            defaults={"name": name, "dimension": dimension, "to_base_factor": to_base, "is_base": is_base},
        )


def unseed_units(apps, schema_editor):
    from inventory.uom import EXTRA_UNITS

    UnitOfMeasure = apps.get_model("inventory", "UnitOfMeasure")
    UnitOfMeasure.objects.filter(company=None, code__in=[u[0] for u in EXTRA_UNITS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0027_merge_semi_finished_into_intermediate"),
    ]

    operations = [
        migrations.RunPython(seed_units, unseed_units),
    ]
