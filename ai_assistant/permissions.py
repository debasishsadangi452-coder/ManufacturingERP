"""Plan gating for AI endpoints.

Per ERP_DOCS/08_subscription_plan_strategy.md, the AI chatbot and all other
AI-powered features are sold only on the Premium AI plan. Role permissions
still apply on top of this — the plan decides what the company purchased,
the role decides what the user may do inside it.
"""
from rest_framework.permissions import BasePermission

from accounts.models import CompanySubscription


class HasPremiumAIPlan(BasePermission):
    """Allow AI endpoints only for companies on an active Premium AI plan.

    Users without a company (legacy/dev accounts created before multi-tenancy)
    are allowed through so local development keeps working. A company that has
    not completed plan selection is blocked — onboarding forces a plan choice.
    """

    message = (
        "AI features are available on the Premium AI plan only. "
        "Upgrade your subscription to use the AI assistant."
    )

    def has_permission(self, request, view):
        company = getattr(request.user, 'company', None)
        if company is None:
            return True
        subscription = CompanySubscription.for_company(company)
        if subscription is None:
            return False
        return subscription.plan == 'premium_ai' and subscription.status == 'active'
