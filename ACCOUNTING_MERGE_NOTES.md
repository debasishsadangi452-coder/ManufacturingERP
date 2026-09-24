# Accounting Branches Merge — Backend (ManufacturingERP)

**Date:** 2026-09-24
**Target:** `main`
**Rollback point:** local tag `pre-accounting-merge` (commit `144036c`)

## What was merged

The accounting feature branches form a linear stack. Each one builds on the previous one. They were merged into `main` one at a time, in order, with `--no-ff`, so every blueprint shows up as its own merge commit:

| # | Branch | Merge commit | Adds |
|---|--------|--------------|------|
| 1 | `feature/accounting-01-foundation` | `80b5c08` | New `accounting` app: chart of accounts, account types, fiscal years, accounting periods, settings, seed data |
| 2 | `feature/accounting-02-erp-context` | `257cd96` | `erp_context.py`: read-only ERP context and telemetry endpoint |
| 3 | `feature/accounting-08-double-entry` | `b27ac68` | `engine.py`: double-entry posting and reversal engine; `JournalEntry`/`JournalEntryLine` |
| 4 | `feature/accounting-09-general-ledger` | `5047784` | `general_ledger.py`: GL, account statement, running balance, trial summary |
| 5 | `feature/accounting-10-accounts-receivable` | `29d899e` | `receivables.py`: AR subledger, aging, customer statements, GL integration |
| 6 | `feature/accounting-11-accounts-payable` | `195ab21` | `payables.py`: AP subledger; `procurement.VendorPayment`, `Bill.amount_paid` |
| 7 | `feature/accounting-12-sales-to-accounting` | `e310978` | `sales_accounting.py`: post/preview/reverse sales invoices and payments to the GL |

There were no merge conflicts. The two commits already on `main` (`core/views.py`, `inventory/views.py`) are unchanged.

## Changes to existing code (outside the new `accounting/` app)

| File | Change | Impact on existing behavior |
|------|--------|-----------------------------|
| `freshfizz_erp/settings.py` | `'accounting'` added to `INSTALLED_APPS` | None. It is a new app. |
| `freshfizz_erp/urls.py` | `path('api/accounting/', ...)` added | None. It is a new URL prefix only. |
| `procurement/models.py` | `Bill.amount_paid` (default `0.00`), a `"partial"` status choice, a `Bill.balance_due` property, `Bill.apply_payment()`, and a new `VendorPayment` model | Additive only. Existing bills get `amount_paid = 0`. No existing code path sets `"partial"`. |
| `accounts/migrations/0009_...` (from the branch) | **Removed after the merge.** It changed the `Company`/`CompanySubscription`/`User` `id` columns to `BigAutoField`, which would rewrite and lock those tables. | Replaced by `default_auto_field = AutoField` in `accounts/apps.py`, which matches the existing integer columns. No schema change. |
| `accounts/apps.py` | Pins `default_auto_field` to `AutoField` | Removes the `accounts` model drift under Django 6.0.2 without touching the DB. |
| `accounting/migrations/0001`, `0002`, `procurement/migrations/0010` | Dependency changed from `accounts.0009` to `accounts.0008` | Needed because `0009` was removed. |
| `procurement/migrations/0010_...` | Adds the `amount_paid` column, the status choices and the `procurement_vendorpayment` table | Additive. |

**How existing processes are protected:**

- The accounting layer does **not** register signals or `ready()` hooks. Nothing in sales, procurement, inventory or QuickBooks posts to the ledger automatically. Posting only happens when a user calls an `/api/accounting/...` endpoint.
- Existing API routes, models and views are unchanged apart from the additive procurement fields above.

## Verification performed

- `manage.py check` passed after **each** of the 7 merges. The only warning is `staticfiles.W004` (missing `static/` directory), which was already there before the merge.
- `manage.py migrate` was run on the local dev database, which holds existing data. A backup was saved as `db.sqlite3.bak-pre-accounting`. All 3 new migrations applied cleanly:
  - `accounting.0001`
  - `accounting.0002`
  - `procurement.0010`
- Test suites for `accounting`, `procurement`, `sales`, `quickbooks`, `accounts` and `inventory`: **143 tests, all passed.**
- `makemigrations --check`: the merge added no new drift, and `accounts` drift is resolved by the `apps.py` pin. The drift that remains in `core`, `finance`, `logistics`, `maintenance`, `quality` and `workforce` (PK `AutoField` → `BigAutoField`) was already on `main` and is out of scope.

## Deployment notes

- Railway runs `python manage.py migrate --noinput` on start, so the 3 migrations apply automatically.
- All 3 migrations are **non-locking in practice**: two create new `accounting_*` tables, and one adds a column with a default to `procurement_bill` plus a new `procurement_vendorpayment` table. No existing table is rewritten, and the user and company tables are not touched.
- Smoke-tested on localhost: the backend on `:8000` and the frontend on `:8080` both started. Existing `/api/sales/` and `/api/procurement/` return 200. `/api/accounting/*` returns 401 without a token, which is expected.
- No new environment variables or Python dependencies are needed.
- To roll back the code: `git reset --hard pre-accounting-merge`. If the migrations have already run, first run `migrate procurement 0009`, then `migrate accounting zero`.
