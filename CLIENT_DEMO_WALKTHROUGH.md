# VGT Manufacturing ERP — Client Demo Walkthrough (Make-to-Order)

**Audience:** the client, during a live demo.
**Goal:** walk the one order **CO‑2026‑001** end to end and show that the ERP
meets the MTO requirements. Requirement numbers in **(req #)** map to the client
requirement list.

**Demo company:** Smarties Foods Manufacturing Pvt. Ltd.
**Login (any role):** password `SmartiesDemo@2026`

| Role | Email | Use for |
|------|-------|---------|
| Admin | `admin@smartiesfoods.com` | Full tour / dashboards |
| Production | `production@smartiesfoods.com` | Planning, WIP, shop floor |
| Quality | `quality@smartiesfoods.com` | QA gates |
| Store | `store@smartiesfoods.com` | Inventory, dispatch |

**The story in one line:** ABC Retail Foods orders **1,000 Smarties Cookies**
(₹7,00,000, due 30 Nov 2026). Production is mid‑run; **300 units are already
finished, shipped, invoiced and part‑paid**, and **700 are still in production** —
so the demo shows a *complete* chain *and* a live partial fulfilment.

---

## Before you start
1. Seed the demo: `./run_demo_seed.sh --local` (safe) or `--deployed`.
2. Start the app (backend + frontend) and open the login page.
3. Log in as **Admin** to begin.

> Tip: keep the narrative on the ONE order. Every screen below is the same
> order, CO‑2026‑001, seen from a different department.

---

## Step 1 — Configurable, MTO‑first architecture  (req #1, #3)
**Screen:** Settings → Manufacturing Settings (`/settings`)
- Show **Business Model = Make‑to‑Order**. Point out this is a *setting*, not
  hard‑coded — the same ERP can be flipped to MTS, QC made mandatory, operation
  sequence enforced, partial dispatch allowed. **(req #3 configurable)**
- Say: *"Nothing starts until a confirmed customer order exists."* **(req #1)**

## Step 2 — The Customer Order Book  (req #4, #5, #6)
**Screen:** Orderbook (`/sales`)  *(formerly "AI Sales Assistant")*
- Open **CO‑2026‑001 / ABC‑ORD‑4587**. Show it was entered **directly as a
  confirmed order** — no forced quotation. **(req #4)**
- Point to **Source = Email** and the fields: customer, item, qty 1,000,
  **promised delivery 30 Nov 2026**, priority High, custom spec
  ("retail packing, halal certified"). **(req #4, #5)**
- Say: *"This is the master order book — every order and its live status in one
  place."* **(req #6)**
- Optional: open **Orders from Email** (`/orders-from-email`) to show
  multi‑channel ingestion. **(req #5)**

## Step 3 — Master data: items, two‑level BOM, UOM, resources  (req #7–#10)
**Screen:** Inventory (`/inventory`) → Items & BOM
- Show the item master has **Raw Materials, a Semi‑Finished "Cookie", and the
  Finished Good "Smarties Cookies"**. **(req #7)**
- Open the **two‑level BOM**: **(req #8)**
  - *Level 1:* Raw Materials → **Cookie (SFG)**
  - *Level 2:* **Cookie + Smarties + Label + Bag + Carton → Smarties Cookies (FG)**
- Show a raw material bought in **kg** but handled in other units — the
  **UOM conversion** (`/unit-setup`). **(req #9)**
- Show **Resources**: machines (Mixer‑01, Oven‑01, Packing‑Machine‑01) and
  manpower (Rahul, Priya, Ankit, Neha). **(req #10)**

## Step 4 — Production Plan from the order  (req #11, #12)
**Screen:** Production (`/production`)
- Open **PP‑2026‑001**, generated from CO‑2026‑001. **(req #11)**
- Show the **BOM explosion**: 1,000 FG → 1,000 Cookie → raw materials
  (White Flour 500 kg, Dates 50 kg, …). **(req #12)**

## Step 5 — MRP shortage → auto purchase  (req #13, #14)
**Screen:** Production (MRP view) → Procurement (`/procurement`)
- Show MRP comparing **required vs available**: everything is covered **except
  Dates — 50 kg needed, 30 kg in stock → short 20 kg** (flagged red). **(req #13)**
- Show this shortage **auto‑created Purchase Requisition PR‑2026‑001**, converted
  to **Purchase Order PO‑2026‑001** (20 kg Dates @ ₹120 to Premium Dry Fruits).
  **(req #14)**

## Step 6 — Receiving + incoming QC  (req #22, #27)
**Screen:** Procurement → Goods Receipt; Quality (`/quality`)
- Show the **GRN** bringing 20 kg Dates into the **Raw Material Warehouse**
  (stock now 50 kg). **(req #22)**
- Open **IQC‑2026‑001**: incoming QC on the Dates — moisture/mould/colour all
  pass → **released** to the RM warehouse. (Reject/Quarantine are the other
  outcomes.) **(req #27)**

## Step 7 — Plan → Production Order, operation‑wise WIP  (req #15–#19)
**Screen:** Production → Shop Floor; Production Manager dashboard
- Show the plan **converted to Production Order PRD‑2026‑001** once materials
  were confirmed. **(req #16)**
- Show the **routing / operations**: Mixing → Baking → Cooling → Smarties
  Addition → Packing → Final Inspection. **(req #17)**
- Show **live WIP**: Mixing ✓, Baking ✓, **Cooling 700/1000 in progress**,
  Packing/Inspection not started — i.e. **300 units still in cooling**. **(req #18)**
- Point out **capacity/resource planning** uses machine run‑rates and manpower
  on each operation. **(req #15)**
- Click through from the order to **"which customer order is this WIP for?"** —
  it traces straight back to CO‑2026‑001. **(req #19)**

## Step 8 — FG QA gate → Finished Goods stock  (req #23, #24, #28)
**Screen:** Quality → Inventory (FG warehouse)
- Explain the **three‑warehouse flow**: Raw Material → Production → Finished
  Goods, and that SFG "Cookie" is tracked distinctly from FG. **(req #20, #23)**
- Show the **FG QA gate**: the completed **300‑unit sub‑batch passed final
  inspection** and only then moved into the **Finished Goods Warehouse**. **(req #28, #24)**

## Step 9 — Allocation, picking, dispatch + Delivery Note  (req #25, #26, #29, #30)
**Screen:** Fulfilment (`/fulfilment`) → Logistics (`/logistics`)
- Show **300 units allocated/reserved** to CO‑2026‑001. **(req #25)**
- Show the picking/packing → **Dispatch DSP‑00001**, 300 units, with
  **logistics**: Road, carrier BlueDart, vehicle MH‑04‑GT‑5521, LR‑MH‑2026‑88214,
  freight ₹4,500. **(req #26, #29, #30)**
- Open the **Delivery Note** document for this dispatch. **(req #38)**

## Step 10 — Invoice → AR → GL  (req #31, #32, #33, #34)
**Screen:** Finance (`/finance`) → Accounting (`/accounting`)
- Show **INV‑xxxx for the 300 dispatched units = ₹2,10,000**, generated off the
  dispatch. **(req #31)**
- Show the **partial payment ₹1,00,000** received → invoice status **Partially
  Paid, balance ₹1,10,000** in **Accounts Receivable**. **(req #32)**
- In Accounting, show the **automatic GL journal entries** for the sale and the
  inventory movements (RM / WIP / FG). **(req #33, #34)**

## Step 11 — Costing (make vs sell)  (req #35)
**Screen:** Production → Costing
- Show the **manufacturing cost** of a Smarties Cookie built from **material +
  machine + labour + overhead**, and that it is **separate from the ₹700 selling
  price**. **(req #35)**

## Step 12 — Documents  (req #36–#40)
**Screen:** any order/production/dispatch → "Print / Document"
- **Order Confirmation** (from CO‑2026‑001) **(req #36)**
- **Production Traveler / Work Ticket** (from PRD‑2026‑001) **(req #37)**
- **Delivery Note** (from the dispatch) **(req #38)**
- **Commercial Invoice** **(req #39)**
- Requisition / QA clearance records back the cross‑department handoffs **(req #40)**

## Step 13 — Dashboards, BI & end‑to‑end traceability  (req #41–#45)
**Screen:** Executive (`/executive`), Order Trace (`/order-trace`), Traceability (`/traceability`)
- **Executive dashboard** — order fulfilment, financial health, pipeline. **(req #41)**
- **Production Manager view** — active orders, WIP, delays, shortages. **(req #42)**
- **Order Trace** — drill CO‑2026‑001 through **Order → Plan → Purchasing → WIP →
  QA → FG → Dispatch → Invoice** on one screen. **(req #43)**
- **Material availability** — RM shortages (Dates), reorder alerts, FG stock. **(req #44)**
- **Integrated BI** — the whole supply chain on one unified view. **(req #45)**

## Step 14 — Exception handling  (req #50)
- The live order **is itself the exception demo**: a **partial production and
  partial dispatch** (300 shipped, 700 still in WIP), partial invoice and
  partial payment. Mention scrap / rework / rejection are supported the same way. **(req #50)**

---

## Closing — the four open points  (req #46–#49)
Tell the client these are **policy choices**, already configurable in the system,
and ask them to confirm:
- **#46** Where exactly RM QC is mandatory (at receiving gate, in storage, or
  before shop‑floor issue).
- **#47** The precise overhead / resource‑cost absorption formula.
- **#48** Capacity definitions (shifts, standard vs actual hours, machine run rates).
- **#49** MRP parameters to include (lead time, MOQ, safety stock, open POs).

## One‑sentence wrap
*"One confirmed customer order drove planning, purchasing, production, quality,
dispatch, invoicing and the ledger — fully traceable end to end — which is
exactly the make‑to‑order ERP you asked for."*
