"""In-process runner for scheduled purchase orders.

Started from wsgi.py, so it only runs inside the web server (gunicorn workers
or runserver) — never in tests or management commands. Each worker polls every
SCHEDULED_ORDERS_POLL_SECONDS (default 300); the atomic claim in
procurement/scheduled.py makes concurrent workers safe. Set the variable to 0 to
disable it, e.g. when `manage.py place_scheduled_orders` runs from cron instead.
"""
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()


def start_scheduler():
    global _started
    try:
        interval = int(os.getenv("SCHEDULED_ORDERS_POLL_SECONDS", "300"))
    except ValueError:
        interval = 300
    if interval <= 0:
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_loop, args=(interval,), name="scheduled-purchase-orders", daemon=True
    ).start()


def _loop(interval):
    from django.db import close_old_connections
    from .scheduled import run_due_scheduled_orders

    time.sleep(min(interval, 30))  # let the server finish booting first
    while True:
        try:
            close_old_connections()
            handled = run_due_scheduled_orders()
            if handled:
                logger.info("Scheduled purchase orders processed: %s", [s.id for s in handled])
        except Exception:
            logger.exception("Scheduled purchase order run failed")
        finally:
            close_old_connections()
        time.sleep(interval)
