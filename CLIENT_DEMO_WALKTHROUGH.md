# VGT Manufacturing ERP
## Client Demonstration Script — Make-to-Order Manufacturing

> **How to use this script.** Present from it top to bottom. Each step gives you
> the **Screen** to open, the **Actions** to perform in order, a **Say** block you
> can read aloud almost verbatim, what to **Show**, and the **Client takeaway**.
> Timings are a guide; the full run is ~30 minutes plus questions. Lines marked
> **⚠ Verify first** are things to confirm on-screen before you state them as fact,
> so you are never caught asserting behaviour the live data does not back.

---

### Demo fact sheet (keep visible to yourself)

| Field | Value |
|-------|-------|
| Demo company | Smarties Foods Manufacturing Pvt. Ltd. |
| Customer | ABC Retail Foods Pvt. Ltd. |
| Reference order | **CO-2026-001** (customer ref **ABC-ORD-4587**) |
| Product | Smarties Cookies (finished good) |
| Order quantity | 1,000 units |
| Selling price | ₹700 / unit → **order value ₹7,00,000** |
| Promised delivery | 30 November 2026 |
| Completed & shipped so far | **300 units** (QA passed, dispatched, invoiced, part-paid) |
| Still in production | **700 units** (currently at the Cooling stage) |
| Partial invoice | 300 × ₹700 = **₹2,10,000** |
| Payment received | **₹1,00,000** → receivable balance **₹1,10,000** |

> The record **numbers** on screen (e.g. the invoice, PO and dispatch IDs) are
> assigned by the database and may not read exactly `PO-2026-001` / `INV-2026-001`.
> Read whatever the screen shows; the *story* is what matters, not the ID text.

### Login accounts (password `SmartiesDemo@2026`)

| Role | Email | Use for |
|------|-------|---------|
| Admin | `admin@smartiesfoods.com` | Full tour, settings, dashboards |
| Production | `production@smartiesfoods.com` | Planning, shop floor, WIP |
| Quality | `quality@smartiesfoods.com` | Incoming & final QA |
| Store | `store@smartiesfoods.com` | Inventory, allocation, dispatch |

---

## Before the meeting — setup (do this in advance)

1. **Seed the demo data:** `./run_demo_seed.sh --local` for a local run, or
   `./run_demo_seed.sh --deployed` to use the hosted database.
2. **Start the app** (backend + frontend) and open the login page.
3. **Log in as Admin.** Keep a second tab on the Executive Dashboard.
4. **Run the Final Checklist** at the bottom of this document once, end to end,
   the day before — so nothing surprises you live.

> **Guiding principle for the whole demo:** keep the narrative on the ONE order.
> Every screen is the same order, **CO-2026-001**, seen from a different
> department. Resist the urge to tour modules in isolation.

---

## Opening — 1 minute

**Screen:** ERP home page / Executive Dashboard.

**Say:**
> "Today I'll demonstrate the manufacturing ERP using one realistic customer
> order. Rather than showing isolated modules, we'll follow the *same* order
> across every department.
>
> Our customer, ABC Retail Foods, has ordered 1,000 Smarties Cookies. The order
> needs raw materials, a two-stage manufacturing process, quality checks, packing
> and final delivery. You'll see how the ERP coordinates all of this, catches a
> material shortage, tracks work in progress, and connects dispatch and invoicing
> right back to the original order."

**Transition:** "Let's start with how the system is set up." → open Manufacturing Settings.

---

## Step 1 — The Make-to-Order business model  ·  1 min  ·  (req #1, #3)

**Screen:** Settings → Manufacturing Settings (`/settings`).

**Actions:**
1. Show **Business Model = Make-to-Order**.
2. Point out the other policy switches: incoming QC required, enforce operation
   sequence, allow partial production & dispatch, overhead method.

**Say:**
> "The system is configured for Make-to-Order. The point is that production is
> driven by confirmed customer demand, not by producing to stock speculatively.
> Notice these are *settings*, not hard-coded behaviour — the same ERP can be
> switched to make-to-stock, QC made mandatory, the operation sequence enforced,
> and partial dispatch allowed or blocked. This order becomes the reference
> transaction for everything downstream."

**Show:** The actual toggles present in the app (don't describe switches that aren't there).

**Client takeaway:** The workflow is demand-driven and configurable to your policies.

---

## Step 2 — Customer order and the Order Book  ·  2 min  ·  (req #4, #5, #6)

**Screen:** Order Book (`/sales`) — *the sales workspace, surfaced as "Orderbook".*

**Actions:**
1. Open **CO-2026-001**.
2. Show the **customer** and **customer reference ABC-ORD-4587**.
3. Show **Source = Email**.
4. Show the ordered **product, quantity 1,000, price ₹700, promised date
   30 Nov 2026, priority High**, and the **custom specification**
   ("retail packing, halal certified").
5. Point out the **order status** and the actions available on it.

**Say:**
> "This order arrived by email and was entered directly as a *confirmed* customer
> order — there's no forced quotation stage. The Order Book gives sales,
> production, procurement and management one shared view of demand and its live
> status. The quantity is 1,000 units, due 30 November. We'll follow this exact
> order for the rest of the demo."

**Optional:** Open **Orders from Email** (`/orders-from-email`) to show multi-channel intake.

**Client takeaway:** One master order book; orders can originate from multiple channels.

**Transition:** "Before we plan it, let's look at what this product is made of." → Items & BOM.

---

## Step 3 — Product structure and manufacturing resources  ·  2 min  ·  (req #7–#10, #20)

**Screen:** Inventory → Items & BOM (`/inventory`); Unit Setup (`/unit-setup`); Resources.

**Actions:**
1. Show the three item categories: **Raw Materials, Semi-Finished Goods,
   Finished Goods**.
2. Open the **two-level BOM**:
   - **Level 1 — make the Cookie (semi-finished):** White Flour, Brown Flour,
     Sugar, Coconut, Dates, Almonds, Flavoring, Artificial Colors → **Cookie**.
   - **Level 2 — make Smarties Cookies (finished):** Cookie + Smarties + Label +
     Bag + Carton → **Smarties Cookies**.
3. Show a **UOM conversion** example (material bought in kg, handled in other units).
4. Show the **Resources**: machines (Mixer-01, Oven-01, Cooling-Rack-01,
   Packing-Machine-01) and operators (Rahul, Priya, Ankit, Neha).

**Say:**
> "This product is manufactured in two stages. First we make the Cookie
> intermediate; then we combine that semi-finished item with Smarties and
> packaging to make the sellable finished product. That two-level structure
> matters because the ERP has to track both the materials consumed *and* the
> intermediate stock produced. We also hold the production resources and units of
> measure the system needs for planning and costing."

**Show:** A real UOM conversion and the actual resource records.

**Client takeaway:** Multi-level product structures, units and resources are modelled properly.

---

## Step 4 — Production planning and material requirements  ·  3 min  ·  (req #11, #12)

**Screen:** Production → Production Plan (`/production`).

**Actions:**
1. Open **PP-2026-001**.
2. Show its **link to CO-2026-001**.
3. Show the requested finished quantity (1,000).
4. Expand the **two-level BOM explosion**.
5. Review the calculated material requirements (e.g. White Flour 500 kg,
   Dates 50 kg, …).

**Say:**
> "Once the order is confirmed, planning turns that demand into a manufacturing
> plan. The BOM explosion works out everything needed to produce 1,000 finished
> units — including the materials for the intermediate Cookie stage. This plan is
> the basis for checking stock, purchasing shortfalls, assessing capacity and
> deciding when production can start."

**Client takeaway:** Planning connects customer demand to actual manufacturing requirements.

---

## Step 5 — MRP shortage and purchasing  ·  3 min  ·  (req #13, #14)

**Screen:** Production → MRP; Procurement (`/procurement`).

**Actions:**
1. Show **required vs available** stock.
2. Highlight **Dates: 50 kg required, 30 kg available, 20 kg short** (flagged).
3. Open the **Purchase Requisition** for the shortfall.
4. Open the **Purchase Order** for 20 kg of Dates (₹120/kg, Premium Dry Fruits Supplier).
5. Show the supplier and the order status.

**Say:**
> "Material planning finds we have enough for most requirements, but Dates is
> short by 20 kilograms. That shortfall becomes a purchasing requirement:
> procurement reviews it, raises the purchase order, and follows the material
> through receipt and inspection. This is what stops production from starting on
> the false assumption that material is already on hand."

**⚠ Verify first:** Only call the requisition "automatically generated" if you've
confirmed that behaviour in this build. Otherwise say "raised from the shortage."

**Client takeaway:** Shortages are caught before production and turned into purchasing action.

---

## Step 6 — Goods receipt and incoming quality  ·  2 min  ·  (req #22, #27)

**Screen:** Procurement → Goods Receipt; Quality (`/quality`).

**Actions:**
1. Open the **goods receipt** for the incoming Dates.
2. Show the **receiving warehouse** (Raw Material Warehouse) and the 20 kg received.
3. Open **IQC-2026-001** (incoming QC).
4. Show the inspection parameters (moisture, mould, colour) and the **release decision (Pass)**.
5. Return to inventory and show the resulting **Dates balance = 50 kg**.

**Say:**
> "Receiving material doesn't automatically make it available for production.
> Incoming quality inspection is a control point first. Here the Dates are
> inspected and released, and only then does the inventory balance update under
> the system's receiving and quality-release rules."

**If supported:** Briefly show what happens to **rejected** or **quarantined** material.

**Client takeaway:** Quality gates the material before it can be consumed.

---

## Step 7 — Production order, scheduling and WIP  ·  4 min  ·  (req #15–#19)

**Screen:** Production → Shop Floor / Production Order.

**Actions:**
1. Open **PRD-2026-001**.
2. Show its **links to the production plan and the customer order**.
3. Review the operations: **Mixing → Baking → Cooling → Smarties Addition →
   Packing → Final Inspection**.
4. Show assigned **machines and operators** per operation.
5. Show planned **capacity / scheduling** (machine run-rates, manpower).
6. Open the **live WIP quantities**.

**Say:**
> "Once material readiness is established, the plan is converted into a production
> order. The shop-floor view shows the sequence of operations and the resources
> assigned to each. Instead of a single status, we see progress *by operation* —
> where the order is waiting or being worked. In this demo, 300 units have
> completed production and passed final inspection, while the remaining 700 are
> still in production, currently at the Cooling stage."

**Demonstrate:** Click from a WIP record **back to CO-2026-001**.

**Client takeaway:** Work in progress is tied to customer demand — not reported as an unconnected factory total.

---

## Step 8 — Final quality and finished-goods inventory  ·  2 min  ·  (req #23, #24, #28)

**Screen:** Quality → Inventory / Finished Goods Warehouse.

**Actions:**
1. Open the **final inspection** for the completed 300-unit batch.
2. Show the **Pass** result.
3. Show the **receipt/transfer into the Finished Goods Warehouse**.
4. Confirm the **quantity available for fulfilment (300)**.

**Say:**
> "Finished goods are released to fulfilment only after the required quality
> checks are complete. The warehouse structure separates raw materials,
> production stock and finished goods, so each stage is tracked independently. The
> 300 inspected units are now available for the customer, while the balance stays
> under production."

**Client takeaway:** A mandatory QA gate stands between production and sellable stock.

---

## Step 9 — Allocation, picking and dispatch  ·  2 min  ·  (req #25, #26, #29, #30, #38)

**Screen:** Fulfilment (`/fulfilment`) → Logistics (`/logistics`).

**Actions:**
1. Open the **customer allocation for 300 units** against CO-2026-001.
2. Show the **picking / packing**.
3. Open the **dispatch (DSP-00001)**.
4. Show the **carrier (BlueDart), mode (Road), vehicle (MH-04-GT-5521), shipment
   reference (LR-MH-2026-88214), freight (₹4,500)**.
5. Open the **Delivery Note** document.

**Say:**
> "Once finished goods are available, the warehouse allocates the quantity to the
> customer order, prepares picking, and records dispatch. This shipment is 300
> units. The dispatch record and Delivery Note capture what was sent, how it
> travelled, and which customer order it fulfils."

**Client takeaway:** Fulfilment and logistics are linked to the order, with proper shipping documents.

---

## Step 10 — Invoice, payment and accounting  ·  2 min  ·  (req #31, #32, #33, #34)

**Screen:** Finance (`/finance`) → Accounting (`/accounting`).

**Actions:**
1. Open the **invoice for the 300 dispatched units**.
2. Confirm the amount: **₹2,10,000**.
3. Show the **₹1,00,000 payment**.
4. Show the **remaining receivable ₹1,10,000** and status **Partially Paid**.
5. Open the corresponding **journal entries** and inventory accounting records.

**Say:**
> "Commercial documents follow fulfilment. The 300 dispatched units generate an
> invoice for ₹2,10,000. After a ₹1,00,000 payment, the outstanding receivable is
> ₹1,10,000. Finance can follow the customer balance and inspect the accounting
> behind it — connecting the physical movement of goods to its financial result."

**⚠ Verify first:** Show the actual posting status and ledger entries. Don't claim
GL postings are automatic unless you've confirmed they posted for this order.

**Client takeaway:** Goods movement and money are connected and auditable.

---

## Step 11 — Manufacturing costing  ·  1 min  ·  (req #35)

**Screen:** Production → Costing.

**Actions:** Show material cost, machine cost, labour cost, overhead, total
manufacturing cost, and unit cost — separate from the ₹700 selling price.

**Say:**
> "Manufacturing cost is distinct from selling price. Material, machine time,
> labour and overhead make up the cost of producing the item; the ₹700 is a
> separate commercial value. Keeping them apart gives management a real basis for
> product economics and order profitability."

**⚠ Verify first:** Confirm the figures the screen actually computes before quoting
any unit cost. Do **not** present a prepared number (e.g. "₹500/unit") as a
system-calculated result unless the displayed calculation matches the records.

**Client takeaway:** Cost to make ≠ price to sell, and the system can show both.

---

## Step 12 — Documents  ·  2 min  ·  (req #36–#40)

**Screen:** From any order / production / dispatch record → "Print / Document".

**Actions — open each one:**
- **Customer Order Confirmation** (from CO-2026-001) — req #36
- **Production Traveler / Work Ticket** (from PRD-2026-001) — req #37
- **Delivery Note** (from the dispatch) — req #38
- **Commercial Invoice** — req #39
- **Requisition / QA clearance** records that back cross-department handoffs — req #40

**Say:**
> "Every handoff produces the document people expect — an order confirmation to
> the customer, a work ticket for the shop floor, a delivery note with the
> shipment, and a commercial invoice. They're generated from the same records, so
> they stay consistent with what actually happened."

**Client takeaway:** The paperwork is produced from live data, not maintained separately.

---

## Step 13 — Dashboards, BI and end-to-end traceability  ·  2 min  ·  (req #41–#45)

**Screen:** Executive Dashboard (`/executive`) → Production Manager view →
Order Trace (`/order-trace`) / Traceability (`/traceability`).

**Actions:**
1. Show overall **order fulfilment and financial indicators** (Executive).
2. Show **active production, WIP, shortages, delays** (Production Manager).
3. Open **CO-2026-001 in Order Trace**.
4. Follow the linked records: **Order → Plan → Procurement → Receipt → Production
   → Quality → Finished Goods → Dispatch → Invoice** on one screen.

**Say:**
> "The real benefit is that departments aren't working from disconnected records.
> Management sees the status of demand, production, materials, fulfilment and
> finance together. And if anyone asks 'what happened to this customer order?',
> the order-trace view lets us follow the linked records instead of reconciling
> spreadsheets by hand."

**Demonstrate:** Open two or three linked records directly from the trace view.

**Client takeaway:** One connected lifecycle with full drill-down traceability.

---

## Step 14 — Exception handling and client decisions  ·  2 min  ·  (req #46–#50)

**Screen:** Use the relevant shortage, QC, WIP or fulfilment screen.

**Actions:** Demonstrate one *verified* exception — the Dates shortage, or the
partial-fulfilment state (300 shipped / 700 in WIP, partial invoice, partial
payment). If a tested rejected-material or rework scenario exists, show it briefly.

**Say:**
> "We deliberately built in a material shortage and a partially fulfilled order,
> to show the ERP representing real operating conditions — not just a perfect
> end-to-end transaction. Before we finalise the business rules, we should agree
> the exact policies for a few points."

**Confirm with the client (the four open points):**
1. **(#46)** At which point is incoming raw-material QC mandatory — at the
   receiving gate, during storage, or before shop-floor issue?
2. **(#47)** How should overhead and machine/labour costs be allocated into unit cost?
3. **(#48)** How are shifts, standard hours, actual hours and machine run-rates defined?
4. **(#49)** Which MRP parameters apply — lead time, minimum order quantity, safety
   stock, and open purchase orders?

**Client takeaway:** The system handles exceptions; these four policies are yours to set.

---

## Closing — 1 minute

**Screen:** Order Trace or Executive Dashboard.

**Say:**
> "We followed one confirmed customer order from demand through planning,
> procurement, manufacturing, quality, dispatch, invoicing and accounting. The
> goal is a connected Make-to-Order process — understand material requirements,
> see production progress, control quality, manage partial fulfilment, and trace
> the financial transactions that result. We'd now like to validate the remaining
> business policies with your team and confirm the workflow matches how you
> actually operate."

---

## Final checklist — run once before the client meeting

- [ ] All four demo accounts can log in (`SmartiesDemo@2026`).
- [ ] The demo dataset is loaded and internally consistent.
- [ ] **CO-2026-001** is linked to **PP-2026-001** and **PRD-2026-001**.
- [ ] The two-level BOM explosion shows correct quantities and units.
- [ ] MRP displays the **20 kg Dates** shortage.
- [ ] The requisition, PO, goods receipt and incoming QC records reconcile.
- [ ] The **300 completed** and **700 remaining** units reconcile across production and fulfilment.
- [ ] The invoice **₹2,10,000**, payment **₹1,00,000** and receivable **₹1,10,000** reconcile.
- [ ] Costing and dashboard values match their source records (⚠ don't quote a figure you haven't seen the system compute).
- [ ] The Delivery Note and other documents open correctly.
- [ ] At least one exception is demonstrated using a verified workflow.
- [ ] Any incomplete or simulated functionality is clearly identified to the client.
