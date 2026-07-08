"""AI message quota tracking (Phase 5 of the subscription plan strategy).

Premium AI companies get `ai_monthly_message_limit` chat messages per
billing period. Usage is stored on CompanySubscription.ai_messages_used
and resets when the billing period rolls over.
"""
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from accounts.models import CompanySubscription


def get_company_subscription(user):
    return CompanySubscription.for_company(getattr(user, 'company', None))


def reset_period_if_lapsed(subscription):
    """Roll the billing period forward and zero the counter once it expires."""
    now = timezone.now()
    if subscription.current_period_end and now > subscription.current_period_end:
        subscription.ai_messages_used = 0
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=30)
        subscription.save(update_fields=[
            'ai_messages_used', 'current_period_start', 'current_period_end',
        ])


def quota_exceeded(subscription) -> bool:
    """True when the company has used up its AI messages for this period.

    A missing subscription (legacy/dev tenant) or a null/zero limit is
    treated as unlimited — plan access itself is enforced separately by
    HasPremiumAIPlan.
    """
    if subscription is None:
        return False
    reset_period_if_lapsed(subscription)
    limit = subscription.ai_monthly_message_limit
    if not limit:
        return False
    return subscription.ai_messages_used >= limit


def consume_ai_message(subscription):
    """Atomically count one AI chat message against the current period."""
    if subscription is None:
        return
    CompanySubscription.objects.filter(pk=subscription.pk).update(
        ai_messages_used=F('ai_messages_used') + 1
    )
