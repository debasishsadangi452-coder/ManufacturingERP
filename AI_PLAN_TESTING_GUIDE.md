# AI Plan Enforcement — Testing Guide

How to verify that all AI features work on the **Premium AI** plan and are
blocked on every other plan, per `ERP_DOCS/08_subscription_plan_strategy.md`.

## What is enforced

| Rule | Where |
|---|---|
| `/api/ai/chat/`, `/api/ai/insights/`, `/api/ai/agents/` return **403** unless the company is on an **active Premium AI** plan | `ai_assistant/permissions.py` (`HasPremiumAIPlan`) |
| `/api/ai/digital-twin/` stays available on all plans (non-LLM operational dashboard) | `ai_assistant/views.py` |
| Chat messages count against `ai_monthly_message_limit` (2000/month); **429** once exhausted; counter resets when the billing period rolls over | `ai_assistant/quota.py` + `ChatView` |
| Floating AI chatbot hidden unless plan is `premium_ai` | `App.tsx` (`PlanGatedChatbot`) |
| `/ai` and `/ai-team` routes redirect to `/` on lower plans | `App.tsx` (`ProtectedRoute requiresAIPlan`) |
| "AI Team" and "AI Automation" sidebar items hidden on lower plans | `Sidebar.tsx` (`aiOnly` flag) |
| Legacy/dev users **without a company** are allowed (backward compatible); a company **without a subscription** is blocked | `HasPremiumAIPlan` |

## 1. Automated tests (fastest)

```powershell
cd ManufacturingERP
..\.venv\Scripts\python.exe manage.py test ai_assistant
```

14 tests in `ai_assistant/tests.py`:

**Plan gating** (`AIPlanGatingTests`)
- Starter / Standard / Professional users get 403 on chat, insights, and agents.
- Premium AI users get 200 on all three (works even with no `GROQ_API_KEY` —
  the API answers with a friendly "not connected" message).
- A cancelled Premium AI subscription is blocked.
- A company that never selected a plan is blocked.
- A legacy user with no company is allowed.
- Anonymous requests get 401.
- Digital twin is *not* plan-gated (403 must never appear for a Professional user).

**Quota** (`AIQuotaTests`, Groq mocked so no real API calls)
- Each chat message increments `ai_messages_used` by 1.
- When `ai_messages_used == ai_monthly_message_limit`, chat returns 429 and the
  LLM is never called.
- A lapsed billing period resets the counter and rolls the period forward 30 days.
- When no API key is configured, no quota is consumed.
- Usage is visible at `GET /api/auth/subscription/`.

Run everything (all apps):

```powershell
..\.venv\Scripts\python.exe manage.py test accounts ai_assistant core inventory production sales finance workforce quality procurement logistics maintenance
```

(Plain `manage.py test` also picks up the `test_*.py` dev scripts in the
project root, which are not unit tests — always pass app labels.)

## 2. Manual API tests (curl / Postman)

Get a token first:

```bash
curl -s -X POST http://localhost:8000/api/token/ \
  -H "Content-Type: application/json" \
  -d '{"username": "<user>", "password": "<pass>"}'
```

| # | Setup | Request | Expected |
|---|---|---|---|
| 1 | User in a **Starter/Standard/Professional** company | `POST /api/ai/chat/` `{"message":"hello"}` | **403** `"AI features are available on the Premium AI plan only..."` |
| 2 | Same user | `GET /api/ai/insights/`, `GET /api/ai/agents/` | **403** |
| 3 | Same user | `GET /api/ai/digital-twin/` | **200** (not plan-gated) |
| 4 | User in a **Premium AI** company | `POST /api/ai/chat/` `{"message":"hello"}` | **200** with an AI reply (or "not connected" if no `GROQ_API_KEY`) |
| 5 | Premium AI, then in Django admin set `ai_messages_used = 2000` on the subscription | `POST /api/ai/chat/` | **429** `"AI quota exceeded"` |
| 6 | Same, plus set `current_period_end` to yesterday | `POST /api/ai/chat/` | **200**; counter reset to 1, new period end ~30 days out |
| 7 | Any plan, no auth header | `POST /api/ai/chat/` | **401** |
| 8 | Premium AI | `GET /api/auth/subscription/` | `ai_messages_used` reflects usage |

To switch a company's plan quickly: log in as that company's admin and
`POST /api/auth/subscription/select/` with `{"plan": "professional"}` (or use
Django admin → Company subscriptions).

## 3. Manual UI tests

| # | As a company on… | Check | Expected |
|---|---|---|---|
| 1 | Starter / Standard / Professional | Bottom-right corner of any page | No floating AI chatbot button |
| 2 | same | Sidebar → Intelligence group | "AI Team" and "AI Automation" are absent; "Digital Twin" still visible (role permitting) |
| 3 | same | Type `/ai` or `/ai-team` in the URL bar | Redirected to the dashboard |
| 4 | Premium AI | Any page | Chatbot button visible; AI Team / AI Automation in sidebar; department links renamed to AI specialists |
| 5 | Premium AI with quota exhausted (see API test 5) | Send a chat message | Error toast — request blocked with 429, no LLM call made |
| 6 | Demo mode (`demo_token`) | Any page | Behaves as Premium AI (by design) |

## Bugs fixed along the way

These blocked the test suite or were stale before this work:

- `inventory/migrations/0006_stockmovement.py` re-created a table that the
  regenerated `0001_initial` already creates — every fresh/test database
  crashed. Now a recorded no-op.
- `production/migrations/0007_remove_recipe_stale_columns.py` used
  Postgres-only `DROP COLUMN IF EXISTS`, crashing SQLite. Now runs only on
  Postgres via `RunPython`.
- `core/tests.py` used the removed `is_finished_good` field, old category
  labels, direct stock creation (now intentionally 403), and production
  orders without the now-required warehouse.
- `inventory/tests.py` used session `force_login` against JWT-only endpoints;
  switched to DRF `force_authenticate`.
