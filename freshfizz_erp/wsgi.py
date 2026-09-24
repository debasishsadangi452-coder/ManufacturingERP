"""
WSGI config for freshfizz_erp project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "freshfizz_erp.settings")

application = get_wsgi_application()

# Places scheduled purchase orders on their date (see procurement/scheduler.py).
from procurement.scheduler import start_scheduler  # noqa: E402

start_scheduler()
