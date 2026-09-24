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

## Follow-up: Automatic GL posting

Operational events now post to the General Ledger automatically. Sales orders and purchase orders are commitments, so they are **not** posted. The books change when goods or money move:

| Event (where it fires) | Journal entry |
|---|---|
| Goods received against a PO (`procurement` goods receipt) | Dr 1210 Raw Materials Inventory / Cr 2050 Goods Received Not Invoiced |
| Vendor bill from a PO (`bills/from_purchase_order`) | Dr 2050 GRNI (or 1210 if the goods weren't received first) / Cr 2010 Accounts Payable |
| Production order completed | Dr 1220 WIP / Cr 1210 Raw Materials, then Dr 1230 Finished Goods / Cr 1220 WIP |
| SO fulfilled, partially fulfilled, or shipment created | Dr 5050 Cost of Goods Sold / Cr 1230 Finished Goods (Cr 1210 for bought-in items with no recipe) |
| Sales invoice generated from an SO | Dr 1100 Accounts Receivable / Cr 4010 Sales |
| Customer payment recorded on an invoice | Dr 1010 Operating Bank / Cr 1100 Accounts Receivable |

Costing uses standard material cost: `Item.purchase_cost`, and for manufactured items the recipe's ingredient cost per finished unit (batch cost ÷ batch size).

**Files**

| File | Change |
|---|---|
| `accounting/auto_posting.py` (new) | Event handlers, `queue_auto_post()`, logging and retry |
| `accounting/models.py`, `migrations/0003_auto_posting.py` | `AccountingSettings.auto_post_enabled` (default on) and a new `AutoPostingLog` table |
| `accounting/views.py`, `serializers.py`, `urls.py` | `GET /api/accounting/auto-posting/`, `POST .../{id}/retry/`, `POST .../retry_failed/`; `auto_post_enabled` added to settings |
| `accounting/seeds.py` | Standard chart gains `2050 GRNI` and `5050 Cost of Goods Sold - Finished Goods`. Companies seeded earlier get these accounts created automatically the first time they're needed. |
| `accounting/payables.py` | Manual bill posting now debits GRNI instead of inventory when the PO's goods receipt was already posted, so inventory isn't counted twice |
| `sales/views.py`, `procurement/views.py`, `production/views.py` | One `queue_auto_post(...)` call after each event above, next to the existing QuickBooks push |
| `accounting/test_auto_posting.py` (new) | 7 end-to-end tests that drive the real endpoints |

**How existing workflows are protected**

- Posting runs with `transaction.on_commit`, **after** the sale, receipt or production run has been saved. It cannot roll back or block them.
- It never raises into the calling view. Every attempt is written to `AutoPostingLog` as posted, skipped or failed. Failed attempts can be retried from Accounting → Accounting Settings.
- It only runs for companies that have a chart of accounts and have `auto_post_enabled` switched on. Companies that don't use the Accounting module see no change.
- It won't post the same event twice. Duplicate events are logged as "skipped".
- Balance updates made while posting don't trigger QuickBooks pushes.

**Verification:** 150 tests pass (the 143 existing tests plus 7 new ones) across accounting, procurement, sales, production, quickbooks, accounts, inventory and finance. The migration was applied to the local DB, and the backend was smoke-tested on localhost.

**Deployment:** one additive migration (`accounting.0003`: one new column with a default, one new table). It does not lock existing tables.

**Known limits:**
- Purchase price variance (the bill total differing from the PO/receipt value) isn't split out.
- Items with no `purchase_cost` post nothing for cost-of-goods and production, and are logged as "skipped".
- Events from before this change are not back-posted. Use the manual AR/AP/Sales tabs for those.
