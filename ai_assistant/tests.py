"""Tests for AI plan gating and the monthly AI message quota.

Per ERP_DOCS/08_subscription_plan_strategy.md:
- The AI chatbot and all AI-powered features are available ONLY on the
  Premium AI plan (Starter / Standard / Professional get HTTP 403).
- Premium AI companies have a monthly AI message quota; requests are
  blocked with HTTP 429 once it is used up, and the counter resets when
  the billing period rolls over.

Run with:  python manage.py test ai_assistant
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Company, CompanySubscription, User
from accounts.plans import PLAN_CONFIG

CHAT_URL = "/api/ai/chat/"
INSIGHTS_URL = "/api/ai/insights/"
AGENTS_URL = "/api/ai/agents/"
DIGITAL_TWIN_URL = "/api/ai/digital-twin/"

NON_AI_PLANS = ["starter", "standard", "professional"]


def make_company_on_plan(name, plan, **subscription_overrides):
    """Create a company subscribed to `plan`, limits copied from PLAN_CONFIG
    exactly like the plan-selection endpoint does."""
    company = Company.objects.create(name=name)
    cfg = PLAN_CONFIG[plan]
    now = timezone.now()
    defaults = dict(
        company=company,
        plan=plan,
        status="active",
        user_limit=cfg["user_limit"],
        warehouse_limit=cfg["warehouse_limit"],
        production_line_limit=cfg["production_line_limit"],
        ai_monthly_message_limit=cfg["ai_monthly_message_limit"],
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )
    defaults.update(subscription_overrides)
    CompanySubscription.objects.create(**defaults)
    return company


def make_user(company, username, role="admin"):
    return User.objects.create_user(
        username=username, password="secret123", role=role, company=company
    )


def mock_groq_client(mock_groq_cls, reply="Hello! How can I help you today?"):
    """Make the patched Groq class return a plain text completion (no tools)."""
    message = MagicMock()
    message.content = reply
    message.tool_calls = None
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    mock_groq_cls.return_value.chat.completions.create.return_value = response
    return mock_groq_cls.return_value


@override_settings(AI_CONFIG={})
class AIPlanGatingTests(APITestCase):
    """Only Premium AI companies may reach the AI endpoints."""

    def test_lower_plans_are_blocked_from_all_ai_endpoints(self):
        for plan in NON_AI_PLANS:
            company = make_company_on_plan(f"{plan} Co", plan)
            user = make_user(company, f"admin@{plan}")
            self.client.force_authenticate(user=user)

            res = self.client.post(CHAT_URL, {"message": "hello"})
            self.assertEqual(res.status_code, 403, f"{plan} should be blocked from chat")
            self.assertIn("Premium AI", str(res.data.get("detail", "")))

            self.assertEqual(self.client.get(INSIGHTS_URL).status_code, 403,
                             f"{plan} should be blocked from insights")
            self.assertEqual(self.client.get(AGENTS_URL).status_code, 403,
                             f"{plan} should be blocked from agents")

    def test_premium_ai_can_use_chat(self):
        company = make_company_on_plan("Prem Co", "premium_ai")
        self.client.force_authenticate(user=make_user(company, "admin@prem"))
        res = self.client.post(CHAT_URL, {"message": "hello"})
        # No GROQ_API_KEY configured in tests: the endpoint still answers 200
        # with a friendly "not connected" message instead of blocking.
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("response", res.data)

    def test_premium_ai_can_list_agents(self):
        company = make_company_on_plan("Prem Co", "premium_ai")
        self.client.force_authenticate(user=make_user(company, "admin@prem"))
        res = self.client.get(AGENTS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(len(res.data.get("agents", [])) > 0)

    def test_premium_ai_can_get_insights(self):
        company = make_company_on_plan("Prem Co", "premium_ai")
        self.client.force_authenticate(user=make_user(company, "admin@prem"))
        res = self.client.get(INSIGHTS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertIn("insights", res.data)

    def test_cancelled_premium_subscription_is_blocked(self):
        company = make_company_on_plan("Lapsed Co", "premium_ai", status="cancelled")
        self.client.force_authenticate(user=make_user(company, "admin@lapsed"))
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 403)

    def test_company_without_subscription_is_blocked(self):
        company = Company.objects.create(name="No Plan Co")
        self.client.force_authenticate(user=make_user(company, "admin@noplan"))
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 403)

    def test_legacy_user_without_company_is_allowed(self):
        # Dev/demo accounts created before multi-tenancy have no company.
        user = User.objects.create_user(
            username="legacy", password="secret123", role="admin"
        )
        self.client.force_authenticate(user=user)
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 200, res.data)

    def test_anonymous_user_is_rejected(self):
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 401)

    def test_digital_twin_is_not_plan_gated(self):
        # The digital twin is a non-AI operational dashboard, available on
        # every plan (only the LLM-powered endpoints are Premium AI only).
        company = make_company_on_plan("Pro Co", "professional")
        self.client.force_authenticate(user=make_user(company, "admin@pro"))
        res = self.client.get(DIGITAL_TWIN_URL)
        self.assertNotEqual(res.status_code, 403)


@override_settings(AI_CONFIG={"GROQ_API_KEY": "test-key", "MODEL": "test-model"})
class AIQuotaTests(APITestCase):
    """Monthly AI message quota for Premium AI companies."""

    def setUp(self):
        self.company = make_company_on_plan("Quota Co", "premium_ai")
        self.subscription = self.company.subscription
        self.user = make_user(self.company, "admin@quota")
        self.client.force_authenticate(user=self.user)

    def refresh(self):
        self.subscription.refresh_from_db()
        return self.subscription

    @patch("ai_assistant.views.Groq")
    def test_chat_message_consumes_quota(self, mock_groq):
        mock_groq_client(mock_groq)
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.refresh().ai_messages_used, 1)

    @patch("ai_assistant.views.Groq")
    def test_request_blocked_when_quota_exhausted(self, mock_groq):
        mock_groq_client(mock_groq)
        self.subscription.ai_messages_used = self.subscription.ai_monthly_message_limit
        self.subscription.save()
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 429)
        self.assertIn("quota", res.data.get("error", "").lower())
        # The blocked request must not call the LLM at all
        mock_groq.return_value.chat.completions.create.assert_not_called()

    @patch("ai_assistant.views.Groq")
    def test_quota_resets_when_billing_period_lapses(self, mock_groq):
        mock_groq_client(mock_groq)
        self.subscription.ai_messages_used = self.subscription.ai_monthly_message_limit
        self.subscription.current_period_end = timezone.now() - timedelta(days=1)
        self.subscription.save()

        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 200, res.data)

        sub = self.refresh()
        self.assertEqual(sub.ai_messages_used, 1)  # reset to 0, then this message
        self.assertGreater(sub.current_period_end, timezone.now())

    @override_settings(AI_CONFIG={})
    def test_no_api_key_does_not_consume_quota(self):
        res = self.client.post(CHAT_URL, {"message": "hello"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.refresh().ai_messages_used, 0)

    @patch("ai_assistant.views.Groq")
    def test_remaining_quota_visible_in_subscription_status(self, mock_groq):
        mock_groq_client(mock_groq)
        self.client.post(CHAT_URL, {"message": "hello"})
        res = self.client.get("/api/auth/subscription/")
        self.assertEqual(res.status_code, 200)
        sub = res.data.get("subscription") or {}
        self.assertEqual(sub.get("ai_messages_used"), 1)
        self.assertEqual(
            sub.get("ai_monthly_message_limit"),
            PLAN_CONFIG["premium_ai"]["ai_monthly_message_limit"],
        )
