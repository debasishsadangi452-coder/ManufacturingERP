from django.apps import AppConfig


class IndianAccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'indian_accounting'
    verbose_name = 'Indian Accounting'

    def ready(self):
        from . import signals  # noqa: F401
