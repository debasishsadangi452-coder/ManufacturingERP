"""Run long QuickBooks jobs (send everything, import) outside the web request.

The production start command runs gunicorn with its defaults (one worker, 30 s
per request). A "send everything" with a real backlog makes one QuickBooks call
per record, outlived that limit, and gunicorn killed the worker: the browser
saw a bare 502 without CORS headers and reported a CORS error, and every other
request queued behind it. Jobs now run in a thread and report progress on
their QuickBooksSyncRun, which the Accounting > QuickBooks tab polls.
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import QuickBooksSyncRun

logger = logging.getLogger(__name__)

# A "running" run with no progress for this long was cut off (worker killed,
# redeploy). Each QuickBooks call times out after 30 s, so a live job always
# beats well within it.
STALE_RUN = timedelta(minutes=3)


def running_job(company):
    """The job in progress for this company, after expiring abandoned ones."""
    QuickBooksSyncRun.objects.filter(
        company=company, status="running", finished_at__isnull=True,
        last_activity_at__lt=timezone.now() - STALE_RUN,
    ).update(status="failed", error_message="Stopped before finishing (the server stopped the job).",
             finished_at=timezone.now())
    return QuickBooksSyncRun.objects.filter(company=company, status="running", finished_at__isnull=True).first()


def start_job(connection, sync_type, job):
    """Create the run, then call job(run) in a background thread (inline when
    QUICKBOOKS_BACKGROUND_JOBS is off, e.g. in tests). Returns the run."""
    run = QuickBooksSyncRun.objects.create(company=connection.company, connection=connection, sync_type=sync_type)
    if not getattr(settings, "QUICKBOOKS_BACKGROUND_JOBS", True):
        _run(job, run)
        run.refresh_from_db()
        return run
    threading.Thread(target=_run, args=(job, run), name=f"quickbooks-{sync_type}", daemon=True).start()
    return run


def _run(job, run):
    close_old_connections()
    try:
        job(run)
    except Exception as exc:
        logger.exception("QuickBooks %s job failed", run.sync_type)
        QuickBooksSyncRun.objects.filter(pk=run.pk, status="running").update(
            status="failed", error_message=str(exc)[:5000], finished_at=timezone.now()
        )
    finally:
        close_old_connections()
