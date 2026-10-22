from django.apps import AppConfig


class AccountsConfig(AppConfig):
    # Pinned to AutoField to match the existing integer PK columns (migration 0008).
    # Django 6.0 defaults to BigAutoField, which would force a locking table rewrite
    # of accounts_user/company/companysubscription and every FK pointing at them.
    default_auto_field = "django.db.models.AutoField"
    name = "accounts"

    def ready(self):
        from django.db.models.signals import post_migrate
        post_migrate.connect(_ensure_superadmin, sender=self)


def _ensure_superadmin(sender, **kwargs):
    # After `migrate`, create the super admin if SUPERADMIN_EMAIL/PASSWORD are set.
    from .superadmin import ensure_superadmin
    ensure_superadmin()
