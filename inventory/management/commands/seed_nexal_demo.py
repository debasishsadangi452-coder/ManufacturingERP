"""Seed the 'Nexal' aluminium welding-wire demo company (Nexalloy products).

    python manage.py seed_nexal_demo [--password <pw>]

A preset of seed_welding_wire_demo, which builds the same demo under any
company name. Re-runnable: the previous 'nexal' demo company is removed first.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed the Nexal aluminium welding-wire demo company with a month of operating and accounting history."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="nexal12345", help="Password for every demo user.")

    def handle(self, *args, **options):
        call_command(
            "seed_welding_wire_demo", company="Nexal", slug="nexal", admin_username="maya.admin@dummy",
            password=options["password"], brand="Nexalloy", sku_prefix="NX", replace=True, stdout=self.stdout,
        )
