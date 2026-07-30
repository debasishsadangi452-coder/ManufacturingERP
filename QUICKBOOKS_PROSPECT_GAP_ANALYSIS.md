# Fit / Gap Analysis — Baking Manufacturer (Discovery Call)

**Prospect profile:** SQF-certified cookie/baking-kit manufacturer. ~4 people. 322 raw line items + 30–40 finished SKUs. Runs on maxed-out Google Sheets today. Uses QuickBooks for accounting. Manufacturing in Mount Kisco NY; ~90% of finished pallets staged in Milton NY for customer pickup; overflow in upstate cold storage.

**What they explicitly want:** stop manual re-keying, handle mixed units of measure (grams/oz/lb), multi-ingredient BOMs, full SQF lot-code traceability, multi-location stock, QuickBooks integration, and — their headline ask — **orders emailed to an inbox get read and entered into the system automatically.**

Legend: ✅ handled · 🟡 partial · ❌ not handled

---

## Summary scorecard

| # | Requirement (their words) | Status | Where it lives / what's missing |
|---|---|---|---|
| 1 | Mixed units of measure — grams + oz + lb, with conversions | ❌ | `Item.unit` / `BOMLine.unit` are free-text `CharField`; no UoM table, no conversion |
| 2 | Multi-ingredient BOMs (9–10+ components incl. packaging) | ✅ | `BOM` / `BOMLine` exist and are wired into onboarding |
| 3 | PO → batch/production math ("50 batches fills the order") | 🟡 | `Recipe`/`ProductionOrder` exist; no batch-size or auto "cases→batches" calc |
| 4 | SQF lot-code traceability (receive → make → ship, forever) | ❌ | `Batch` model exists but is **orphaned** — not linked to production, QC, or shipment |
| 5 | Multi-location + cold storage stock | 🟡 | `Warehouse`/`Stock` are multi-location; no transfer or in-transit/staging concept |
| 6 | QuickBooks integration | ✅ | Full two-way sync already built (customers, items, invoices, bills, etc.) |
| 7 | **Email inbox → auto-create order** | ❌ | No email ingestion anywhere in the codebase |
| 8 | Bill of Lading / shipping paperwork for traceability | ❌ | `Shipment` has driver/vehicle text fields; no BOL, no document generation |
| 9 | Roles: admin / ops manager / QC / ops coordinator | 🟡 | Roles exist (`admin, production, quality, sales, store, …`) but don't match their org; no "operations coordinator" |
| 10 | Perpetual inventory (kill the 2-hr Friday count) | 🟡 | `StockMovement` + auto-deduct exist; needs cycle-count / reconciliation UI to replace the manual count |

---

## Detailed findings

### 1. Units of measure — ❌ THE dealbreaker they named
> *"a combination of grams, ounces, and pounds… that is challenging for most ERP systems… no system seems to be able to handle."*

**Current state:** `inventory/models.py` — `Item.unit = CharField(default="unit")`, and `BOMLine.unit = CharField(default="unit")`. It's a text label. There is **no unit-of-measure table, no base unit, and no conversion factor** anywhere in the code.

**What their spreadsheet does that we don't:** they store "the way the product comes" plus **extra columns translating it into other units** so the MRP math works. To replace that we need:
- A `UnitOfMeasure` model (name, base unit, conversion factor to base).
- Purchase UoM vs. stock/recipe UoM on each item, with automatic conversion.
- BOM lines that consume in recipe units but deduct stock in the item's base unit.

**Verdict:** This is the single most important build. If UoM conversion isn't solved, the rest doesn't matter to them — it's the reason every prior ERP failed for them.

### 2. Bill of Materials — ✅ already there
> *"chocolate chip has nine different [ingredients]… aluminum pan, film, master case, pads (8), case label, blue tape, pallet."*

`BOM` (one per finished good) + `BOMLine` (raw material, quantity, unit) handle arbitrary multi-component recipes **including packaging** as raw-material line items. Already integrated into the QuickBooks onboarding wizard. **Packaging-as-BOM-line is supported today.** The only weakness is the `unit` field (see #1).

### 3. PO → production batch math — 🟡 partial
> *"if this is the PO, then this is how many batches we have to make… we'll make 50 batches of cookies and cream and that'll fill the order."*

`production/models.py` has `Recipe`, `RecipeIngredient`, `ProductionOrder(quantity)`. Raw materials can be reserved/deducted. **Missing:** a batch-size concept and the automatic "X cases ordered → Y batches to produce" calculation. Today `ProductionOrder.quantity` is a raw number a human sets; the cases→batches translation they do on a second sheet isn't automated.

### 4. Lot-code traceability — ❌ biggest SQF gap
> *"we have to trace every single cookie back… shipping records forever… receiving lot codes… we made this much, we shipped this much, all aligned with lot codes."*

**Current state:** a `Batch` model exists (`item, batch_number, expiry_date, quantity`) but it is **completely orphaned** — grep shows no reference to batch/lot from `ProductionOrder`, `QualityCheck`, `SalesOrderItem`, or either `Shipment` model. So there is **no chain** linking received-material lot → production batch → QC → shipped lot.

**What SQF needs and we lack:**
- Lot/batch captured at **goods receipt** (raw material).
- Production order **consumes specific lots** and **produces a finished lot**.
- QC check **references the lot**.
- Shipment records **which finished lots** went to which customer.
- Immutable, permanent retention.

**Verdict:** Their heaviest recurring paperwork burden, and it's essentially greenfield. High-value, non-trivial build.

### 5. Multi-location & cold storage — 🟡 partial
> *"90% goes to Milton NY, customer picks up… also sending to cold storage upstate."*

`Warehouse` + `Stock (item, warehouse, quantity)` already model stock per location. **Missing:** inter-warehouse **transfers**, and a notion of "staged for pickup at Milton" vs. "in cold storage" vs. "at the plant." The locations can exist; the movement of finished pallets between them, and pickup tracking, is not modeled.

### 6. QuickBooks — ✅ strong fit
> *"potentially integrates into QuickBooks, our accounting software."*

Already the most mature area. Two-way OAuth sync of customers, vendors, items, estimates (sales orders), invoices, bills, payments; auto-push on ERP writes; onboarding wizard; and (added recently) a 24-hour scheduled pull. **This is a selling point, not a gap.**

### 7. Email-to-order ingestion — ❌ their headline ask, entirely unbuilt
> *"All of our POs come to an inbox, then get manually [entered]… instead of going to orders@… go to whatever email we set up… reads what comes into orders and just puts it in the system."*

**Current state:** grep for imap/inbox/mailbox/email-parse/PO-ingest across the whole codebase returns **nothing**. There is no mailbox listener, no PO parser, no draft-order pipeline.

**What it takes:** a mailbox connector (IMAP/forwarding address) → an AI/LLM extraction step (they already attempt this in sheets) → a **draft** SalesOrder for human confirmation (not blind auto-create — SQF + accuracy demands a review step). The AI plumbing (`ai_assistant`, Groq) partly exists, so extraction is feasible, but the ingestion pipeline is greenfield.

**Verdict:** This is the demo that wins the deal. It's also the one thing they asked for most directly and it does not exist yet.

### 8. Bill of Lading / shipping docs — ❌
> *"create the bill of ladings… all the shipping paperwork that goes with that because of traceability."*

`Shipment` (sales + logistics) tracks status, driver, vehicle as **plain text**. No BOL model, no document/PDF generation, no link to lot codes. All greenfield.

### 9. Roles vs. their org — 🟡 mismatch
Their team: **Admin (owner) → Operations Manager (production planning) → QC Manager (traceability) → Operations Coordinator (ordering, receiving, shipping, inventory, e-commerce — "touches everything").**

Our `ROLE_CHOICES`: `admin, hr, store, production, quality, sales, finance`. Closest mapping: ops manager→`production`, QC→`quality`, ops coordinator→`store` (+ needs sales/logistics). **The all-important "Operations Coordinator" doesn't have a matching cross-functional role**, and there's no HR/finance need for them. Roles are configurable but would need tailoring.

### 10. Perpetual inventory — 🟡 partial
> *"stuck doing a two-hour inventory every Friday… physical double-check on every shipment."*

`StockMovement` (IN/OUT/ADJUST) and auto-deduction on production/procurement exist, which is the foundation for perpetual inventory. **Missing:** a cycle-count / physical-reconciliation workflow and a per-shipment pick-verification step — the two manual rituals they want to eliminate.

---

## What to build, in priority order

**Phased roadmap at a glance:**

- **P0 — win the deal:** UoM conversions + email-to-draft-order. *No UoM = no deal, and email ingestion is the winning demo.*
- **P1 — the SQF layer:** lot-traceability chain + Bill of Lading generation.
- **P2 — kill the manual work:** cases→batches auto-calc, warehouse transfers, cycle counts.
- **P3 — polish:** role model tailored to their 4-person org.

The table below expands each phase with the specific gap (#), effort, and rationale.

| Priority | Item | Effort | Why |
|---|---|---|---|
| **P0** | Unit-of-measure model + conversions (#1) | High | The reason every prior ERP failed them. No UoM = no deal. |
| **P0** | Email inbox → AI-extracted **draft** orders (#7) | High | Their headline ask + the winning demo. AI groundwork partly exists. |
| **P1** | Lot-code traceability chain: receive→produce→QC→ship (#4) | High | Their heaviest SQF burden; currently orphaned `Batch` model. |
| **P1** | Bill of Lading + shipping doc generation (#8) | Medium | SQF paperwork; pairs with #4. |
| **P2** | Cases→batches auto-calc + batch size (#3) | Medium | Removes the "second sheet" production math. |
| **P2** | Inter-warehouse transfers + pickup/cold-storage staging (#5) | Medium | Milton staging + cold storage movement. |
| **P2** | Cycle-count / reconciliation + shipment pick-verify (#10) | Medium | Kills the Friday 2-hr count and per-shipment double-check. |
| **P3** | Role model tailored to their 4-person org (#9) | Low | Add "Operations Coordinator"; drop unused roles. |

**Already a strength to lead with:** QuickBooks (#6) and multi-component BOMs (#2) are built and demo-ready today.

---

## Appendix — Sample "Orders from Email" Sales Order list (concept only)

> **Not built yet. This is an illustrative mockup for the P0 email-ingestion feature (#7), to show the prospect the intended experience.** The idea: POs sent to a dedicated address (e.g. `orders@theircompany`) are read, the AI extracts the order, and it lands here as a **draft** for one-click human confirmation — never blind auto-creation (SQF + accuracy).

**Sales Orders — Inbox view**

| SO # | Source | From (email) | Customer | Order lines (product × cases) | Total cases | Pickup date | Confidence | Status |
|---|---|---|---|---|---|---|---|---|
| SO-1042 | 📧 Email | buyer@costco.com | Costco NE | Cookies & Cream ×200 · Choc PB ×300 | 500 | 2026-09-15 | 98% | 🟡 Draft — review |
| SO-1041 | 📧 Email | orders@freshmart.com | FreshMart | Baking Kit (Choc Chip) ×120 | 120 | 2026-08-05 | 91% | 🟡 Draft — review |
| SO-1040 | 📧 Email | jane@sweettooth.co | Sweet Tooth Co | Cookies & Cream ×50 · Snickerdoodle ×40 | 90 | 2026-08-02 | 72% | 🔴 Needs attention — qty unclear |
| SO-1039 | 📝 Manual | — | Whole Foods NY | Choc PB ×150 | 150 | 2026-08-01 | — | ✅ Confirmed |
| SO-1038 | 📧 Email | buyer@costco.com | Costco NE | Baking Kit (S'mores) ×80 | 80 | 2026-07-28 | 95% | ✅ Confirmed → in production |

**Legend / what each column demonstrates**

- **Source** — 📧 pulled from the inbox vs. 📝 keyed by hand. The whole pitch is that most rows say "Email."
- **From (email)** — the original sender, so the operations coordinator can open the source message to verify against the parsed order.
- **Confidence** — how sure the AI was about the extraction. High (≥90%) = quick confirm; low (SO-1040 at 72%) = flagged red because a quantity was ambiguous in the email.
- **Status flow** — `Draft → Confirmed → In production`. Nothing hits production or QuickBooks until a human confirms. This is the review step that replaces "Monica emails the team."

**Row detail (what opening SO-1042 would show)**

```
SO-1042  ·  🟡 Draft — review          [ View original email ]  [ Confirm order ]

  From:     buyer@costco.com  ·  received 2026-07-29 09:14
  Customer: Costco NE  (matched to existing QuickBooks customer ✓)
  Pickup:   2026-09-15  ·  Milton NY staging

  Line items (AI-extracted):
    Cookies & Cream          200 cases      → 50 batches to produce
    Chocolate Peanut Butter  300 cases      → 75 batches to produce
                             ───────────
    Total                    500 cases      125 batches

  ⚠ Confirm before this creates a production schedule or syncs to QuickBooks.
```

*This ties three gaps together for the demo: email→order (#7), the cases→batches auto-calc (#3), and the existing QuickBooks customer match (#6). Build order and effort are in the priority table above.*

---

## P0 Implementation Plan

The two P0 items are independent and can be built in parallel. Both are additive — they extend existing models rather than rewrite them, so current QuickBooks/BOM behavior is preserved.

### P0-A · Units of Measure + conversions

**Goal:** replace the free-text `unit` string with real units that convert, so a raw material bought in pounds, stocked in grams, and consumed in a recipe in ounces all reconcile automatically — exactly the "translation columns" their spreadsheet does by hand.

**Data model (new + changed):**

| Change | Detail |
|---|---|
| **New** `UnitOfMeasure` | `code` ("g", "oz", "lb", "case", "each"), `name`, `dimension` (mass / count / volume), `to_base_factor` (e.g. oz → 28.3495 g), `is_base` per dimension. Company-scoped or a shared seed set. |
| **Change** `Item` | add `base_unit` (FK → UoM, the unit stock is held in) and `purchase_unit` (FK → UoM, how it's bought). Keep the old `unit` string temporarily for backfill. |
| **Change** `BOMLine` | replace text `unit` with FK → UoM; add a `convert()` so recipe qty deducts stock in the item's `base_unit`. |
| **Change** `PurchaseOrderItem` / `GoodsReceipt` | receive in `purchase_unit`, convert to `base_unit` on the resulting `StockMovement`. |

**Core logic:** one `convert(qty, from_uom, to_uom)` helper (guards same-dimension only — grams↔each must raise, not silently coerce). Every stock-affecting path (`BOMLine`, production deduction, goods receipt, `StockMovement`) routes quantities through it.

**Migration & backfill (the risky part — call it out):**
1. Ship UoM table + FKs as **nullable**, data migration seeds standard units (g/oz/lb/kg/case/each with factors).
2. Backfill: map each existing `Item.unit` string → a UoM. The 322 rows won't map cleanly (free text like "unit", "bag", "cs"), so this needs a **review screen**, not a blind script. Unmapped items default to `each` and are flagged.
3. Only after backfill, make FKs non-null and retire the string field.

**QuickBooks safety:** QBO items are quantity-only (no UoM concept), so the push layer keeps sending base-unit quantities — no change to the existing sync contract. Verify against sandbox before/after.

**Effort:** High. ~1 new model, 4 model changes, 1 conversion module, a backfill/review UI, and regression tests on every stock path. Estimate 2–3 weeks incl. the backfill UI.

### P0-B · Email inbox → AI-extracted draft orders

**Goal:** a PO emailed to a dedicated address becomes a **draft** SalesOrder here, pre-filled, for one-click human confirmation. Never auto-confirm (SQF + their own "too much room for error").

**Pipeline (4 stages):**

1. **Ingest** — a mailbox connector. Two options, recommend starting with (a):
   - (a) **Forwarding address** — they forward `orders@` to an address we poll via IMAP on a schedule (reuse the same scheduled-command pattern as the QuickBooks 24h sync). Simplest, no OAuth.
   - (b) Gmail/Graph API webhook — realtime but heavier (OAuth per mailbox). Phase 2.
2. **Extract** — feed the email body + any attachment (PDF/CSV) to the existing Groq LLM layer (`ai_assistant`) with a structured-output prompt: customer, line items (product + cases), pickup date. Return a **confidence score** per field. This is the piece they already attempt in sheets, so it's a proven approach.
3. **Match & stage** — fuzzy-match extracted product names → `Item`, customer → `Customer` (reuse QuickBooks customer links). Create a `SalesOrder` in a new **`draft`** status with the parse attached (raw email + confidence).
4. **Review** — the "Orders from Email" list in the appendix above. Human confirms → order moves to `confirmed`, which triggers the *existing* downstream flow (production planning, QuickBooks estimate push). Low-confidence rows flagged red.

**Data model:**

| Change | Detail |
|---|---|
| **New** `InboundOrderEmail` | raw message, sender, received-at, attachment ref, parse JSON, confidence, link to created `SalesOrder`. Audit trail (SQF-friendly). |
| **Change** `SalesOrder` | add `"draft"` to `STATUS_CHOICES` and a `source` field (`email` / `manual`). Draft orders are excluded from the QuickBooks push until confirmed. |

**Reuses what exists:** the scheduled-command pattern (from the QB sync work), the Groq agent plumbing, and the QuickBooks customer/item links for matching. Extraction is feasible today; the ingest + staging + review pipeline is the new build.

**Guardrail:** draft orders must **not** hit the `post_save` QuickBooks auto-push — gate the push on `status != "draft"` so an unconfirmed parse never syncs. (Same care as the `suppress_auto_push` logic already in `quickbooks/push.py`.)

**Effort:** High. Mailbox poller + parser + matcher + 2 models + review UI + push guardrail. Estimate 2–3 weeks; a rough "email → draft appears in list" demo is achievable much sooner and is the deal-winning moment.

### Suggested sequence

1. **Week 1–2:** P0-B mailbox→draft happy path (the demo). In parallel, P0-A UoM model + seed + conversion helper.
2. **Week 2–3:** P0-A backfill/review UI for the 322 items; P0-B confidence flagging + confirm→downstream wiring.
3. **Before launch:** regression pass on stock paths (P0-A) and the QuickBooks push guardrail (P0-B), verified against the QB sandbox.

**Sequencing note:** UoM (P0-A) is the harder *technical* build and the true dealbreaker; email-to-draft (P0-B) is the more visible *demo*. Build the P0-B happy path first to win the room, but don't let it ship to production ahead of UoM — an order system that can't reconcile grams/oz/lb isn't usable for them.

---

*Grounded in the current codebase (models across `inventory`, `production`, `quality`, `sales`, `logistics`, `accounts`, `quickbooks`, `ai_assistant`). "❌ not handled" means no supporting model/logic was found, not merely a missing UI.*
