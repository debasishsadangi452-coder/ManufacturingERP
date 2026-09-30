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

## Follow-up: Fiscal year lifecycle

- **The current year is the default.** The first time a company lists its fiscal years (or seeds its chart of accounts), the current calendar year is opened automatically with its 12 monthly periods and set as the current fiscal year.
- **Only one open year at a time.** Creating a fiscal year while another is open is refused, and so is reopening an old year while a later one is open. Both return a clear error.
- **The next year is only available after closing.** The new `POST /api/accounting/fiscal-years/start_next_year/` opens the 12-month year that starts the day after the latest year ends. It handles both calendar years (`FY 2027`) and Apr–Mar style years (`FY 2026-2027`). It creates the periods and makes the new year current.
- A company's first year can still be created with custom dates (for example Apr–Mar). Any later year must start the day after the previous one ends.
- Files: `accounting/seeds.py` (lifecycle helpers; `generate_monthly_periods` is shared with the existing `generate_periods` action), `accounting/views.py`, `accounting/test_fiscal_year_lifecycle.py` (5 tests).
- There are no migrations or model changes. The rules are enforced in the API, so fiscal years that already exist are not altered.

## Follow-up: Add Client from Sales

- The backend already supported creating customers. This adds `sales/test_customers.py` (2 tests), which confirms that a new client is always assigned to the user's own company and that a name is required.

## Follow-up: Vendor assignment and finished-good selling prices

- **Assigning ingredients to vendors.** This uses the existing `VendorPriceList` model and API: vendor + raw material + quoted unit price, minimum order and lead time, with one entry per vendor per ingredient. `procurement/serializers.py` now checks that the vendor and item belong to the requesting user's company. Before this, another company's IDs were accepted. Tests are in `procurement/test_vendor_prices.py` (2).
- **Setting selling prices.** Previously a finished good's selling price could only be set when the item was created, and Sales users had no permission to edit items. The new `GET /api/sales/price-list/` returns every finished good with its selling price and standard cost. `PATCH /api/sales/price-list/{item_id}/` updates the **selling price only**. Sales, Finance and Admin can use it, and every change is written to the activity log. The item save pushes the new price to QuickBooks through the existing item sync. Tests are in `sales/test_price_list.py` (3).
- There are no migrations.

## Follow-up: Scheduled purchase orders (AI Procurement)

Users can set an ingredient, a quantity and a date, and the purchase order is placed automatically on that date. A schedule can also repeat weekly or monthly.

- **Model:** `procurement.ScheduledPurchaseOrder`, created by migration `procurement.0011` (one new table; existing tables are untouched).
- **Placing** (`procurement/scheduled.py`):
  - On the date, a real PO is raised at the chosen vendor's quoted price. If no vendor is chosen, the cheapest vendor quote on file that day is used.
  - The PO is set to `ordered` and the vendor email is drafted, exactly like ordering by hand, when the person who scheduled it may approve that amount (an admin, or anyone within their `auto_approve_limit`). Otherwise the PO is raised as `pending` and admins are notified, so scheduling can't bypass approvals.
  - Repeating schedules move on to their next date. Monthly repeats keep their day of the month (for example the 31st becomes the 30th in April). If the server was down for a while, only one catch-up order is placed.
  - Failures, such as no vendor price on file, mark the schedule `failed` with the reason and notify the store. The user can fix the problem and click "place now", or edit the schedule to re-arm it.
- **Runner:** `procurement/scheduler.py` is a background thread started from `wsgi.py`, so it only runs inside the web server (never in tests or management commands). It checks every `SCHEDULED_ORDERS_POLL_SECONDS` (default 300).
  - Each schedule is claimed with an atomic UPDATE, so multiple gunicorn workers can never place it twice.
  - Setting the variable to `0` disables the thread. You can then run `python manage.py place_scheduled_orders` from cron instead.
  - The date follows `TIME_ZONE = "UTC"`, so orders are placed from 00:00 UTC (05:30 IST) on their date.
- **API:** `/api/procurement/scheduled-orders/` supports list, create and edit, DELETE (which cancels and keeps history), and `POST {id}/place_now/`. It is open to Store and Admin users and scoped to the company.
- **AI Procurement:** new tools `schedule_procurement` (for requests like "order 500 kg sugar on the 1st of next month", optionally weekly or monthly) and `list_scheduled_procurements`. Today's date is now added to every AI agent's system prompt, so relative dates resolve correctly.
- **Tests:** `procurement/test_scheduled_orders.py` (8). I also verified it live: a server started against a copy of the DB placed a due schedule by itself (PO-0071, `ordered`, 40 × $2.00).

---

# Second merge: Accounting Blueprints #13–#21 (2026-09-26/27)

**Rollback point:** local tag `pre-accounting-merge-2`.

## What was merged

As before, the branches were merged one at a time with `--no-ff`. `feature/accounting-13-purchase-to-accounting` is contained in `feature/accounting`, so it went first.

| Step | Branch | Adds |
|---|---|---|
| 1 | `feature/accounting-13-purchase-to-accounting` | `purchase_accounting.py`: post, preview and reverse vendor bills with input tax (`/api/accounting/purchases/`) |
| 2 | `feature/accounting` (#14–#21) | #14 Inventory-to-Accounting, #15 Manufacturing-to-Accounting, #16 Expenses, #17 Cash & Bank, #18 Payments & Allocations, #19 Tax layer, #20 Journal approvals, audit and attachments, #21 Period closing and year-end closing entry. Adds migrations `accounting` 0003–0010. `inventory/serializers.py` adds an accounting status to stock movements. |

None of the new modules post automatically; each one posts only when a user clicks.

## Conflicts and fixes made while merging

- **Migrations:**
  - The branch's migrations depended on `accounts/0009`, the migration removed earlier because it rewrote and locked the user and company tables. They now depend on `accounts/0008`.
  - `main`'s `0003_auto_posting` is renumbered to `0011_auto_posting`, so it runs after the branch's `0003`–`0010`. It had never been deployed.
  - There's no migration drift for `accounting`.
- **`models.py`, `serializers.py`, `views.py`:** the branch's version was used as the base, and `main`'s changes were re-applied on top: `auto_post_enabled`, `AutoPostingLog`, auto-posting API, fiscal-year lifecycle and `start_next_year`.
- **Fiscal year:**
  - The branch's stricter `close_year` is kept. Every month must be closed first, and it generates a year-end closing entry.
  - The branch's rule that reopening needs a reason is kept, plus `main`'s rule that only one year can be open.
- **Payments summary (branch bug):** money totals came back as `"5000"` instead of `"5000.00"` on SQLite. They are now formatted consistently.

## Reconciling automatic posting with the new subledgers (no double posting)

| Event | Before | Now |
|---|---|---|
| Production completed | Auto-posting wrote its own entry (`production.order`). The Manufacturing tab could post the same order again (`manufacturing`). | Auto-posting calls `post_manufacturing_accounting`. There is one entry per order, and the Manufacturing tab shows it as posted. It respects `manufacturing_accounting_enabled` and the labour and overhead policy. |
| Goods receipt | Auto-posted (`procurement.receipt`). The Inventory tab could post the same `GRN PO#…` stock movement again. | The Inventory module treats those movements as "already accounted (auto_posted)". Auto-posting also skips a receipt whose movements were already posted by hand. |
| Sales shipment | Auto-posted COGS (`sales.shipment`). The Inventory tab could post the `Fulfilled / Partial fulfillment / Shipment SO#…` movements again. | Handled the same way. `Reserved for SO#… production` movements are deliberately **not** matched. References must match exactly, so `SO#1` never matches `SO#12`. |
| Vendor bill posted by hand (Purchase-to-Accounting) | Debited Raw Materials again even if the receipt was auto-posted to inventory. | Clears GRNI (2050) when the receipt was auto-posted, in both the preview and the post. |
| Stock movement list (branch) | The accounting-status lookup was **not scoped by company**, so one company could see another company's journal number for a matching ID. | Scoped to the movement's company. |

**Tests:** `accounting/test_subledger_reconciliation.py` (4 tests, new). `test_auto_posting.py` is updated for the Manufacturing module delegation.

---

# Third merge: Accounting Blueprints #26–#27 (2026-09-29)

**Rollback point:** local tag `pre-accounting-merge-3`.

- #22–#25 (Financial Reports and three more) were already merged into `main` on 2026-09-28, outside this session. Only #26 (UI architecture) and #27 (roles and permissions) were new.
- **Backend:**
  - Adds `accounting/ui_architecture.py`, `roles_permissions.py` and `permissions.py`, plus two read-only endpoints for Finance and Admin: `/api/accounting/ui-architecture/` and `/api/accounting/roles-permissions/`.
  - The new permission classes are **not** applied to any existing endpoint, so nobody's access changes.
  - There are no migrations.
  - The one conflict was in `views.py`, where both sides appended a section at the end. Both are kept.
- **Frontend:**
  - Adds `RolesPermissionsTab` and `UIArchitectureTab`. It merged cleanly.
  - Fixed a bug from the #22 merge: `FinancialReportsTab` called `getAccountingPeriods(fyId)` with a bare ID, so the period picker loaded every period rather than the chosen year's. It now passes `{ fiscal_year: fyId }`.
  - The frontend is back to the 12 old type errors, and `npm run build` passes.
- **`seo/crawlable-landing-pages` was not merged, on purpose.** Its only commit (`6e2746a`) is identical (same patch-id) to `d67397e`, which is already on `main`, and `main` has four follow-up commits improving those pages since. Merging would add nothing and would risk the old version overwriting the newer pages during conflict resolution. It is safe to delete on GitHub.

## Follow-up: Base prices always settable from Sales and Procurement

- `/api/sales/price-list/`: Store users, who can raise sales orders, may now **read** base selling prices. Changing them is still limited to Sales, Finance and Admin. A test covers this in `sales/test_price_list.py`.

## Follow-up: Vendor bill created automatically on goods receipt

- **New `procurement/billing.py`:**
  - `create_bill_from_po` is shared with the existing `bills/from_purchase_order` API.
  - `on_goods_received(po, receipt, user)` is the single hook every receiving path calls: the Procurement **Receive Goods** screen, the AI Procurement "receive PO" tool and the assistant's `create_goods_receipt` tool.
  - The hook auto-posts the receipt (Dr 1210 / Cr 2050). When **`AccountingSettings.auto_bill_on_receipt`** is on (the default), it also creates the vendor bill from the PO, pushes it to QuickBooks if connected, and auto-posts it (Dr 2050 GRNI / Cr 2010 AP).
  - The bill's due date comes from the vendor's payment terms (`net30` gives 30 days, "due on receipt" gives 0).
  - A PO that already has a bill is not billed again.
  - Companies with no Accounting setup keep the old behaviour, with no automatic bill.
  - Failures never block the receipt.
- **AI receipts:** receipts recorded through the AI tools were not auto-posted before. They are now.
- **Migration:** `accounting.0012_auto_bill_on_receipt` (one new column with a default; no table rewrite).
- **Tests:** `procurement/test_auto_bill.py` (5). The manual-billing test now switches auto-billing off, and the reports test relies on the automatic bill.

## Follow-up: Payment status per purchase order, and paying from Procurement

- `PurchaseOrderSerializer.billing` is a read-only summary of each order's vendor bill: state (not_billed / open / partial / overdue / paid), totals, due date and last payment date.
- New `POST /api/procurement/purchase-orders/{id}/pay_bill/`, **admins only**. It calls the same `record_and_allocate_ap_payment` as Accounting → Accounts Payable, in one transaction:
  - the bill becomes partial or paid;
  - a `VendorPayment` is recorded;
  - Dr 2010 Accounts Payable / Cr 1010 Bank is posted.
  - If the ledger refuses the posting (closed period, no chart of accounts), nothing is saved.
- **Tests:** `procurement/test_po_payment.py` (6). These cover: status per order, full and partial payment, overpayment refused, store users can see but not pay, no bill means no payment, and the rollback when the period is closed.

## Follow-up: QuickBooks as the main books

QuickBooks is the company's main set of books, and the ERP keeps local books. Every ERP accounting record now reaches QuickBooks **exactly once**:

| ERP activity | Reaches QuickBooks as |
|---|---|
| Invoices, bills, customer payments (existing) | The QuickBooks document; QuickBooks books it itself |
| **Vendor bill payments** (new) | A `BillPayment` linked to the bill, paid from the bank account mapped to 1010 (or the first QuickBooks bank account) |
| **Customer payments from every screen** (new) | A `Payment`, sent on creation through a `CustomerPayment` signal. This covers Accounts Receivable and Payments & Allocations allocations, not only Sales. |
| **Undone payments** (new) | The QuickBooks payment is deleted when the ERP payment row is deleted (for example when an allocation is undone) |
| **Manual journals, expenses, cash & bank, tax adjustments, and reversals of these** (new) | A `JournalEntry`, sent when posted, using the account mapping |
| Goods receipts, cost of goods sold, production, inventory valuation, year-end closing | **Not sent.** QuickBooks derives these from its Inventory items, the mirrored stock quantities, and its own year-end close. Sending them would double-count. |

**Details:**
- **Account mapping:** new model `QuickBooksAccountMapping`. Auto-mapping matches by name first, then by account type. If an account can't be mapped, the entry is not sent and the error says which account to map.
- **Payments:** the Payments-module `Payment` record itself is **not** sent. Its per-invoice or per-bill allocations already create `CustomerPayment`/`VendorPayment` rows, and those are what gets sent. Sending both would record the money twice. Unallocated advances reach QuickBooks once they're allocated.
- **Storing QuickBooks IDs:** `_store_result` now writes IDs with an `UPDATE` instead of `save()`. A posted journal entry in a since-closed period would otherwise fail validation on save.
- **Push all:** also sends bill payments and ERP-only journal entries.
- **New endpoints:** `/api/quickbooks/overview/`, `accounts/`, `account-mappings/` (GET/POST), `account-mappings/auto/` and `retry-errors/`. The OAuth return path now allows `/accounting`.
- **Removed:** the duplicate explicit payment push in `sales/views.py`; the signal handles it now.
- **Migrations:** `accounting.0013`, `procurement.0012` and `quickbooks.0003`. They add QuickBooks ID columns and the mapping table, all additive.
- **Tests:** `quickbooks/test_main_books.py` (9). All 36 QuickBooks tests pass.
- **Date fix found while testing:** Sales (payment date, invoice due date), the Procurement bill date and `billing.py` used the machine's local date (`date.today()`), while reports and the rest of the app use Django's date (`timezone.localdate()`, UTC). On an IST machine between 00:00 and 05:30, a payment was dated "tomorrow", so the Balance Sheet left it out. All four places now use `timezone.localdate()`, and the tests written this session do the same. On Railway both clocks are UTC, so production wasn't affected.

## Fix: "Send everything to QuickBooks" failing in production with a CORS error

- **Cause** (confirmed by the Railway log): the start command runs gunicorn with its defaults, one worker and a **30-second** request limit. "Send everything" makes one QuickBooks call per unsent record, so with a backlog it ran past 30 seconds. Gunicorn aborted the worker (`handle_abort` → `SystemExit: 1`), the browser got a bare 502 with no CORS headers and reported it as a CORS error, and every other request queued behind it. CORS itself was fine (`CORS_ALLOW_ALL_ORIGINS = True`).
- **Fix:** new `quickbooks/background.py`. The `push-all/` endpoint now starts the job in a background thread and returns **202** with its sync run straight away. `sync/` does the same when called with `"background": true`, which the Accounting tab does; the onboarding wizard still gets immediate results.
  - Only one job runs per company at a time. Starting another returns **409** "already running".
  - A run still marked "running" after 30 minutes (for example cut off by a redeploy) is marked failed, so it doesn't block new jobs.
  - `retry-errors/` handles 50 records per click and returns how many remain.
- **Setting:** `QUICKBOOKS_BACKGROUND_JOBS` (default on; `0` runs jobs inside the request, as before).
- **Tests:** 5 new cases in `quickbooks/test_main_books.py`. All 41 QuickBooks tests pass.
- **Recommended (not changed):** add `--workers 3 --timeout 120` to the gunicorn start command in `railway.json`, so any other slow request (such as the onboarding imports) doesn't hold up the whole app.
