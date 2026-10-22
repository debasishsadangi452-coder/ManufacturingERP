from django.core.management.base import BaseCommand

from accounts.superadmin import ensure_superadmin


class Command(BaseCommand):
    help = "Create the platform super admin from SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD if it does not exist."

    def add_arguments(self, parser):
        parser.add_argument("--reset-password", action="store_true", help="Set the password again even if the user exists.")

    def handle(self, *args, **options):
        user, created = ensure_superadmin(reset_password=options["reset_password"])
        if user is None:
            self.stderr.write("Set SUPERADMIN_EMAIL and SUPERADMIN_PASSWORD in the environment first.")
            return
        self.stdout.write(self.style.SUCCESS(f"{'Created' if created else 'Found'} super admin {user.username}"))
