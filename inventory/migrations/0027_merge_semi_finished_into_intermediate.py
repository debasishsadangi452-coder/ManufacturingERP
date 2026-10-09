"""Merge the legacy "semi_finished" item category into "intermediate".

Semi-finished goods are stored under the single value "intermediate" (labelled
"Semi-Finished" in the UI). A few records may still carry the legacy
"semi_finished" string; this migration folds them into "intermediate" so there
is one canonical value. Both `category` and `erp_classification` are converted.
"""

from django.db import migrations


def merge_forward(apps, schema_editor):
    Item = apps.get_model("inventory", "Item")
    db = schema_editor.connection.alias
    Item.objects.using(db).filter(category="semi_finished").update(category="intermediate")
    Item.objects.using(db).filter(erp_classification="semi_finished").update(
        erp_classification="intermediate"
    )


def merge_backward(apps, schema_editor):
    # One-way merge: "intermediate" is the canonical value and there is no
    # information to tell which rows were once "semi_finished", so reversing is
    # a no-op rather than a lossy guess.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0026_alter_item_category_alter_item_erp_classification"),
    ]

    operations = [
        migrations.RunPython(merge_forward, merge_backward),
    ]
