from django.db import migrations


def drop_stale_columns(apps, schema_editor):
    # The stale columns only ever existed on the Railway Postgres DB, and
    # "DROP COLUMN IF EXISTS" is Postgres-only syntax — running it on SQLite
    # (local dev / test databases) crashes with a syntax error.
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        ALTER TABLE production_recipe
            DROP COLUMN IF EXISTS effective_from,
            DROP COLUMN IF EXISTS effective_to,
            DROP COLUMN IF EXISTS is_active,
            DROP COLUMN IF EXISTS notes,
            DROP COLUMN IF EXISTS version;
        """
    )


class Migration(migrations.Migration):
    """
    The Railway DB was created with extra columns on production_recipe
    (effective_from, effective_to, is_active, notes, version) that were
    never part of the Django model. Drop them so inserts no longer fail
    with a NOT NULL violation.
    """

    dependencies = [
        ("production", "0006_add_materials_reserved"),
    ]

    operations = [
        migrations.RunPython(drop_stale_columns, migrations.RunPython.noop),
    ]
