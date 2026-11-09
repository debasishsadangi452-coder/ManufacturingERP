"""Login tracking: which IP address each sign-in attempt came from."""
import ipaddress
import logging

from django.db.models import Q

from .models import LoginEvent, User

logger = logging.getLogger(__name__)


def client_ip(request):
    """Best guess at the caller's IP, or None.

    The app runs behind a proxy, so REMOTE_ADDR is the proxy. Prefer X-Real-IP, then
    the left-most X-Forwarded-For entry, then REMOTE_ADDR. Headers can be forged by
    a caller, which is why the raw chain is stored next to the result."""
    meta = request.META
    forwarded = [p.strip() for p in meta.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    for candidate in [meta.get("HTTP_X_REAL_IP", "").strip(), *forwarded, meta.get("REMOTE_ADDR", "").strip()]:
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def record_login(request, typed_username, user, success):
    """Store one sign-in attempt. Never raises: tracking must not break signing in."""
    try:
        typed = (typed_username or "").strip()[:254]
        if user is None and typed:
            user = User.objects.filter(Q(username__iexact=typed) | Q(email__iexact=typed)).order_by("id").first()
        LoginEvent.objects.create(
            user=user,
            username=typed,
            success=success,
            ip_address=client_ip(request),
            forwarded_for=request.META.get("HTTP_X_FORWARDED_FOR", "")[:500],
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Could not record login event")
