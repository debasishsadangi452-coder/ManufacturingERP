"""Test settings: skip the migration history (which has pre-existing
inconsistencies, e.g. inventory_stockmovement created twice) and build the
test database directly from the current models.

Usage: python manage.py test --settings=freshfizz_erp.test_settings
"""
from .settings import *  # noqa: F401,F403


class DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = DisableMigrations()

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
