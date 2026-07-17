from django.apps import AppConfig


class QuickbooksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "quickbooks"

    def ready(self):
        from . import signals  # noqa: F401
