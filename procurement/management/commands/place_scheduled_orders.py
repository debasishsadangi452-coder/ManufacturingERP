"""Place every scheduled purchase order that is due.

The web server already does this every few minutes (procurement/scheduler.py).
Use this command instead when that is disabled, e.g. from a cron job:

    python manage.py place_scheduled_orders
"""
from django.core.management.base import BaseCommand

from procurement.scheduled import run_due_scheduled_orders


class Command(BaseCommand):
    help = "Place scheduled purchase orders whose date has arrived."

    def handle(self, *args, **options):
        handled = run_due_scheduled_orders()
        for schedule in handled:
            self.stdout.write(f"#{schedule.id} {schedule.status}: {schedule.last_message}")
        self.stdout.write(self.style.SUCCESS(f"{len(handled)} scheduled order(s) processed."))
