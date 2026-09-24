from django.apps import AppConfig


class AccountsConfig(AppConfig):
    # Pinned to AutoField to match the existing integer PK columns (migration 0008).
    # Django 6.0 defaults to BigAutoField, which would force a locking table rewrite
    # of accounts_user/company/companysubscription and every FK pointing at them.
    default_auto_field = "django.db.models.AutoField"
    name = "accounts"
