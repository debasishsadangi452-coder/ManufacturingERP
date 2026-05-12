# 📖 The FreshFizz Chronicles
## *The Complete Testing Odyssey of a Digital Beverage Empire*

> This is not just a testing document. It is the story of a company's first day—written for a new employee, a curious tester, and a discerning engineer. Follow the journey from the first login to the last financial report. Along the way, every button, every rule, and every edge case will be tested.

---

## 🎭 The Full Cast of Characters

Before the curtains rise, meet the crew. Each character has a **login**, a **domain of power**, and a wall of rules they cannot cross.

| Character Name | Their Role | Login | Password | Powers | Forbidden Actions |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **The Founder** | Admin | `admin_user` | `password123` | Full system access. Edit, delete, override anything. | Nothing is forbidden. |
| **The Talent Scout** | HR Manager | `hr_user` | `password123` | Hire, fire, manage payroll, training & attendance. | Cannot see inventory, POs, or sales revenue. |
| **The Quartermaster** | Store Manager | `store_user` | `password123` | Create POs, receive goods, manage warehouses, dispatch. | Cannot hire/fire, cannot approve budgets. |
| **The Alchemist** | Production Mgr | `production_user` | `password123` | Create recipes, schedule batches, start maintenance. | Cannot create sales orders or see customer data. |
| **The Guardian** | Quality Ctrl | `quality_user` | `password123` | Submit & approve/reject QC tests. | Cannot schedule production batches. |
| **The Hustler** | Sales | `sales_user` | `password123` | Manage customers, create & confirm sales orders. | Cannot see COGS data, recipes, or payroll. |
| **The Treasurer** | Finance Mgr | `finance_user` | `password123` | Approve expenses, set budgets, view P&L reports. | Cannot receive physical inventory. |
| **The Oracle** | AI Assistant | *(Chatbox)* | — | Cross-module queries and automated actions with confirmation. | Cannot bypass RBAC constraints. |

---

## 🌅 Chapter 1: The Sunrise — Getting Ready

Every story starts with a sunrise. Before we can build anything, the engines must be started.

### 1.1 Starting the Engines
1.  Open terminal #1 in the `ManufacturingERP` folder.
    ```
    .venv\Scripts\python.exe manage.py runserver
    ```
    *You should see:* `Watching for file changes with StatReloader`
2.  Open terminal #2 in the `brewmaster-ui` folder.
    ```
    npm run dev
    ```
    *You should see:* `VITE v5.x ready in XXX ms → Local: http://localhost:8080/`
3.  Open your browser and navigate to `http://localhost:8080`.
4.  Log in as **The Founder** — `admin_user` / `password123`.

### 1.2 The First Impression (UI Check)
- ✅ The dashboard loads with charts, KPIs, and navigation tiles.
- ✅ The sidebar links are all visible.
- ✅ A welcome banner or notification panel is present.
- ✅ The page title reads "FreshFizz ERP" or "BrewMaster."

### 1.3 Role Boundary Checks (RBAC Smoke Test)
Before building anything, verify the locks are in place.
1.  Log out. Log in as `store_user`.
2.  Try to navigate to **Workforce** > **Payroll**.
    - **Expected**: Access Denied or that page/tile is hidden from the sidebar.
3.  Log out. Log in as `hr_user`.
4.  Try to navigate to **Procurement** > **Purchase Orders**.
    - **Expected**: Page not visible or a permission error.
5.  Log out. Return as `admin_user` for the next chapter.

---

## 🏗️ Chapter 2: Laying the Foundations — Inventory & People

**The Founder** and **The Talent Scout** co-operate to build the factory and fill it with talent.

### 2.1 Building the Warehouses (Admin)
1.  Navigate to **Inventory** > **Warehouses** tab.
2.  Click **Add Warehouse**.
    - **Name**: `North Manufacturing Plant`
    - **Location**: `Industrial Zone, Chicago`
    - Click **Create**.
    - **Expected**: Warehouse appears in the list. It has zero stock across all items.
3.  Click **Add Warehouse** again.
    - **Name**: `South Distribution Center`
    - **Location**: `Port Complex, Miami`
    - Click **Create**.
    - **Expected**: Two warehouses are now visible.

### 2.2 Registering Inventory Items (Admin)
1.  Navigate to **Inventory** > **Items** tab.
2.  Click **Add Item**.
    - **Name**: `Pure Sugar`
    - **Category**: `Raw Material`
    - **Unit**: `kg`
    - **Create**.
3.  Click **Add Item**.
    - **Name**: `Cola Concentrate`
    - **Category**: `Raw Material`
    - **Unit**: `Liters`
    - **Create**.
4.  Click **Add Item**.
    - **Name**: `Freshfizz Cola 500ml`
    - **Category**: `Finished Good`
    - **Unit**: `Bottles`
    - **Create**.
    - **Expected**: All three items visible in the catalog. Filter by "Raw Material" — only Sugar and Concentrate appear.

### 2.3 Building the Workforce (HR Manager)
> 🔒 Log out as Admin. Log in as **`hr_user`**.

1.  Navigate to **Workforce** > **Employees** tab.
2.  Click **Add Employee**.
    - **First Name**: `Samantha`
    - **Last Name**: `Rivera`
    - **Email**: `s.rivera@freshfizz.com`
    - **Role**: `Line Supervisor`
    - **Shift**: `Morning`
    - **Department**: `Production`
    - Click **Hire**.
    - **Expected**: Employee ID `EMP-0001` (or next available) is auto-generated. Record appears.
3.  Click **Add Employee**.
    - **Name**: `Marcus Chen`
    - **Role**: `Quality Analyst`
    - **Shift**: `Afternoon`, **Department**: `Quality Control`.
    - **Hire**.
4.  Click **Add Employee**.
    - **Name**: `Rita Patel`
    - **Role**: `Logistics Coordinator`
    - **Shift**: `Morning`, **Department**: `Logistics`.
    - **Hire**.

### 2.4 Shift Assignment (HR Manager)
1.  Navigate to **Workforce** > **Shifts** tab.
2.  Find the `Morning Shift`. Click **Assign Employees**.
3.  Add `Samantha Rivera` and `Rita Patel` to the Morning Shift.
    - **Expected**: Both employees listed under Morning Shift participants.

### 2.5 Attendance — Clocking In (HR Manager)
1.  Navigate to **Workforce** > **Attendance** tab.
2.  Find `Samantha Rivera`. Click **Clock In**.
    - **Expected**: Samantha's record now shows a Check-In timestamp. Status: "Active".
3.  **Edge Case**: Try to Clock In Samantha a second time.
    - **Expected**: System rejects it — "Already clocked in." or the button is disabled.

### 2.6 Leave Request (HR Manager)
1.  Find `Marcus Chen` > Click **Request Leave**.
    - **Leave Type**: `Annual Leave`
    - **Start**: Tomorrow's date. **End**: Day after.
    - **Reason**: "Family vacation".
    - Submit.
    - **Expected**: Leave request appears with status "Pending".
2.  Back in the list, click **Approve** on Marcus's request.
    - **Expected**: Status changes to "Approved". Marcus's calendar shows dates blocked.

### 2.7 Training Assignment (HR Manager)
1.  Navigate to **Workforce** > **Training** tab.
2.  Click **New Training Program**.
    - **Name**: `Food Safety & Hygiene`,  **Type**: `Safety`, **Mandatory**: ✅
    - **Due Date**: 30 days from today.
3.  Enroll `Samantha Rivera` and `Marcus Chen`.
    - **Expected**: Both employees show "In Training" tag in their profile.

---

## 📦 Chapter 3: The Cargo Arrives — Procurement

**The Quartermaster** stands at the gates of the factory with an empty warehouse and a mission.

### 3.1 Creating Vendors (Store Manager)
> 🔒 Log out. Log in as **`store_user`**.

1.  Navigate to **Procurement** > **Vendors**.
2.  Click **Add Vendor**.
    - **Name**: `Global Agribiz`
    - **Category**: `Raw Materials`
    - **Email**: `orders@globalagribiz.com`
    - **Phone**: `+1-555-9900`
    - Click **Create**.
3.  Add another vendor: `FlavourLab Inc`, category `Beverages`.

### 3.2 Vendor Price Lists (Store Manager)
1.  Open vendor `Global Agribiz` > **Price List** tab.
2.  Click **Add Price**.
    - **Item**: `Pure Sugar` | **Unit Price**: `$0.50` | **Min Order**: `500` | **Lead Time**: `5 days`.
3.  Open `FlavourLab Inc` > **Price List**.
    - **Item**: `Cola Concentrate` | **Price**: `$15.00` | **Min Order**: `50` | **Lead Time**: `3 days`.

### 3.3 Creating a Purchase Order (Store Manager)
1.  Navigate to **Procurement** > **Purchase Orders** > **New PO**.
2.  **Select Vendor**: `Global Agribiz`.
3.  Add line item: `Pure Sugar`, Qty: `2000`.
    - **Expected**: Unit price auto-fills from price list ($0.50). Total = $1,000.00.
4.  Click **Create PO**.
    - **Expected**: PO is created with status `Draft`. PO Number auto-assigned (e.g., `PO-001`).

### 3.4 Approving & Receiving the PO (Store Manager)
1.  Find PO-001. Click **Submit for Approval**.
    - Status changes to `Pending`.
2.  Click **Approve**.
    - Status changes to `Approved`.
3.  Click **Receive Goods**.
    - **Select Warehouse**: `North Manufacturing Plant`.
    - Click **Confirm Receipt**.
    - **Expected**: Status becomes `Received`. Navigate to **Inventory**. `Pure Sugar` at `North Manufacturing Plant` now reads `2,000 kg`. A `StockMovement (IN)` record is created.

### 3.5 Second PO — Concentrate (Store Manager)
1.  Create a PO for vendor `FlavourLab Inc` → `Cola Concentrate` qty `200L`.
2.  Approve and Receive it into `North Manufacturing Plant`.
    - **Expected**: Concentrate stock reads `200 L`.

### 3.6 Edge Case — RBAC on Procurement (HR User)
1.  Log out. Log in as `hr_user`. Try to create a Purchase Order.
    - **Expected**: Procurement section is inaccessible or "New PO" button is hidden/disabled.
2.  Log back in as `store_user`.

---

## 🏭 Chapter 4: The Alchemist's Ritual — Production

Bright lights flicker on in the production hall. **The Alchemist** walks in carrying ancient scrolls — the recipes.

### 4.1 Defining the Recipe (Production Manager)
> 🔒 Log out. Log in as **`production_user`**.

1.  Navigate to **Production** > **Recipes** tab.
2.  Click **Define Recipe**.
    - **Product**: `Freshfizz Cola 500ml`.
    - Click **Create Recipe**.
3.  On the newly created recipe, click **Add Ingredient**.
    - **Item**: `Pure Sugar`, **Qty per Unit**: `0.2` (i.e., 0.2 kg per bottle).
4.  Click **Add Ingredient**.
    - **Item**: `Cola Concentrate`, **Qty per Unit**: `0.05` (0.05 L per bottle).
    - **Expected**: Recipe card shows 2 ingredients. Any production run using this will require 0.2 kg Sugar and 0.05L Concentrate per bottle.

### 4.2 Verifying the Production Lines (Admin)
> (Switch back to Admin for line setup if needed)

1.  Navigate to **Production** > **Production Lines** tab.
2.  Confirm `Line A` exists and its status is `Running`.
    - If not: Click **New Line**. Name: `Line A`, Capacity: `100 units/hr`.

### 4.3 Scheduling a Batch (Production Manager)
> Log in as `production_user`.

1.  Navigate to **Production** > **Timeline** or **New Batch**.
2.  Fill in the form:
    - **Recipe**: `Freshfizz Cola 500ml`
    - **Quantity**: `1000 Bottles`
    - **Line**: `Line A`
    - **Warehouse**: `North Manufacturing Plant`
    - **Start Time**: Set to current time.
3.  Click **Schedule Batch**.
    - **Expected**:
        - A new production order `PO-PROD-001` is created with status `Scheduled`.
        - The Gantt/Timeline chart shows a block for `Line A`.
        - In **Inventory**, the `Pure Sugar` reserve column reflects 200 kg (1000 * 0.2) — it's not taken yet, just reserved.

### 4.4 Starting and Completing Production (Production Manager)
1.  Navigate to **Production** > **Orders List**. Find `PO-PROD-001`.
2.  Click **Start Batch**.
    - **Expected**: Status changes to `Running`.
3.  Click **Complete Batch**.
    - **Expected**:
        - Status → `Completed`.
        - **Inventory Check**: `Pure Sugar` -200 kg (from 2000 to 1800). `Cola Concentrate` -50 L (from 200 to 150).
        - `Freshfizz Cola 500ml` +1000 Bottles.

### 4.5 The Machine Crisis (Production Manager)
1.  Navigate to **Maintenance** > **Equipment**.
2.  Click **Add Equipment**.
    - **Name**: `Carbonator Unit 3`, **Line**: `Line A`.
3.  Manually set its **Health** to `25%` to simulate a failing machine.
4.  Click **Add Task**.
    - **Equipment**: `Carbonator Unit 3`, **Type**: `Corrective`, **Priority**: `High`.
    - **Description**: "Seal failure causing gas leakage."
5.  Click **Start Task**, then **Complete Task**.
    - **Expected**: Machine health returns to `100%`. Status changes from `Breakdown` to `Idle` or `Running`.

### 4.6 Scheduling Preventive Maintenance (Production Manager)
1.  On `Carbonator Unit 3`, click **Add Task**.
    - **Type**: `Preventive`, **Priority**: `Low`, **Scheduled Date**: 30 days from today.
    - **Expected**: A future task is queued. The machine shows "Next Maintenance Due: [Date]".

### 4.7 Edge Case — RBAC on Batches (Quality User)
1.  Log out. Log in as `quality_user`. Try to navigate to **Production** > **New Batch**.
    - **Expected**: "New Batch" button is hidden. Quality users can only view, not schedule.

---

## 🛡️ Chapter 5: The Guardian's Verdict — Quality Control

The Guardian arrives at the output line and holds up a hand. "No bottle leaves without my seal."

> 🔒 Log out. Log in as **`quality_user`**.

### 5.1 Creating a Quality Test (Quality Controller)
1.  Navigate to **Quality Control**.
2.  Click **New Test**.
    - **Production Order**: `PO-PROD-001`
    - **Test Type**: `Brix Level` (Sugar content)
    - **Parameter**: `Sugar %`, **Target**: `11.0`, **Result**: `11.2`.
    - **Status**: `Approved` (because 11.2 is within tolerance).
    - Click **Submit**.
    - **Expected**: Test record created. Linked to Batch #PROD-001.

### 5.2 Multi-Parameter Testing (Quality Controller)
1.  Click **New Test** again on the same batch.
    - **Test Type**: `CO2 Pressure Level`
    - **Result**: `40 psi`, **Target**: `38 psi`, Status: `Pass`.
2.  And one more:
    - **Test Type**: `pH Level`, **Result**: `3.1`, **Target**: `3.0–3.5`, Status: `Pass`.
    - **Expected**: Three distinct test records for the same batch.

### 5.3 The Failing Test (Quality Controller — Edge Case)
1.  Create a second batch. Navigate to **Production**, schedule `Batch #PROD-002` for 500 bottles (as `production_user`), and complete it.
2.  Back as `quality_user`, create a test for Batch #PROD-002.
    - **Test Type**: `Foreign Particle Check`, **Result**: `Glass fragment detected`, **Status**: `FAIL`.
    - **Submit**.
    - **Expected**: Batch #PROD-002 is locked. Verify by logging in as `sales_user` and trying to sell the failed batch.
    - **Expected**: System shows "Batch QC FAILED — Cannot be shipped."

### 5.4 Approving the Good Batch (Quality Controller)
1.  Find the test records for Batch #PROD-001. Click **Approve** on the batch.
    - **Expected**: Batch #PROD-001 status becomes `Quality Approved`. Ready for shipment.

---

## 🚢 Chapter 6: The Great Deal — Sales

**The Hustler** has been getting calls all morning. A customer is ready to buy.

> 🔒 Log out. Log in as **`sales_user`**.

### 6.1 Creating a Customer (Sales)
1.  Navigate to **Sales** > **Customers** tab.
2.  Click **Add Customer**.
    - **Name**: `Mega Retail Group`
    - **Email**: `orders@megaretail.com`
    - **Phone**: `+1-800-5432`
    - **Address**: `88 Commerce Ave, Atlanta`.
    - Click **Create**.
    - **Expected**: Customer profile created and visible in the list.

### 6.2 Raising a Sales Order (Sales)
1.  Navigate to **Sales** > **New Sales Order**.
2.  **Customer**: `Mega Retail Group`.
3.  Click **Add Item**.
    - **Item**: `Freshfizz Cola 500ml`, **Qty**: `750 Bottles`.
4.  Click **Create Order**.
    - **Expected**: SO-001 is created with status `Pending`. Total amount calculated.

### 6.3 Confirming the Order (Sales)
1.  Find SO-001. Click **Confirm Order**.
    - **Expected**: Status changes to `Confirmed`.

### 6.4 Edge Case — Overselling (Sales)
1.  Create another Sales Order for `Mega Retail Group`, Qty: `5000 Bottles` (which exceeds the 1000 in stock).
    - **Expected**: System warns "Insufficient inventory" or flags the order as `Backordered`. The order is not processed.

### 6.5 RBAC Check — No Recipe Access for Sales (Sales)
1.  While still logged in as `sales_user`, try to navigate to **Production** > **Recipes**.
    - **Expected**: The Production section is NOT visible in the sidebar, OR clicking it shows "Unauthorized."

---

## 🚚 Chapter 7: The Journey — Logistics

The factory floor smells of fresh cola. Now the bottles must reach their destination.

> 🔒 Log out. Log in as **`store_user`**.

### 7.1 Fleet Registration (Store Manager / Admin)
1.  Navigate to **Logistics** > **Vehicles**.
2.  Click **Add Vehicle**.
    - **Name**: `Delivery Van Alpha`, **Type**: `Standard`, **Capacity**: `1000`, **Driver**: `Jack Davis`.
    - Status: `Available`.
3.  Add another: `Refrigerated Truck Beta`, Type: `Refrigerated`, Capacity: `5000`, Driver: `Priya Nair`.

### 7.2 Creating a Delivery Route (Store Manager)
1.  Navigate to **Logistics** > **Delivery Routes** > **New Route**.
    - **Name**: `Atlanta Metro Run`
    - **Stops**: `3` (Warehouse → Retailer → Hub)
    - **Distance**: `120 km`
    - **Estimated Time**: `3 hours`
    - **Assigned Vehicle**: `Delivery Van Alpha`.
    - Click **Create**.

### 7.3 Dispatching the Shipment (Store Manager)
1.  Navigate to **Sales** > **Orders**. Find `SO-001 — Mega Retail Group`.
2.  Click **Create Shipment**.
    - **Warehouse**: `South Distribution Center`.
    - **Driver**: `Jack Davis`, **Vehicle**: `Delivery Van Alpha`.
    - **Priority**: `High`.
3.  Click **Start Route** on the `Atlanta Metro Run`.
    - **Expected**: Shipment status → `In Transit`. Vehicle Alpha status → `In Use`.

### 7.4 Confirming Delivery (Store Manager)
1.  Find the Shipment. Click **Mark as Delivered**.
    - **Expected**: Shipment status → `Delivered`. SO-001 status → `Delivered`. Inventory at `South Distribution Center` drops by 750 bottles.

---

## 💰 Chapter 8: The Final Count — Finance

As the trucks return and the factory quiets, **The Treasurer** opens the ledger.

> 🔒 Log out. Log in as **`finance_user`**.

### 8.1 Department Budget Setup (Finance)
1.  Navigate to **Finance** > **Department Budgets**.
2.  Click **Set Budget**.
    - **Department**: `Production`
    - **Period**: `Monthly`, **Label**: `March 2026`
    - **Total Budget**: `$50,000`
    - **Auto-Approve Limit**: `$2,000` (Expenses below this are auto-approved).
3.  Repeat for `Procurement` department: Budget `$100,000`, Auto-Approve `$5,000`.

### 8.2 Reviewing Expense Requests (Finance)
1.  Navigate to **Finance** > **Expenses**.
2.  Any pending expense should be listed here. Click **Approve** on the maintenance/utility charge.
    - **Expected**: Status → `Approved`. Department budget "Remaining" column decreases.

### 8.3 Auto-Approval Logic Test (Finance)
1.  As any user, submit a small expense:
    - **Title**: `Printer Ink`, **Amount**: `$45.00`, **Category**: `Miscellaneous`.
    - Link to `Production` budget (March 2026).
    - Click **Submit**.
    - **Expected**: Since $45 < $2,000 (auto-approve limit) AND there is sufficient budget, status immediately becomes `Auto Approved` — no manual review needed.
2.  Now submit a large expense:
    - **Title**: `New Conveyor Belt`, **Amount**: `$35,000`.
    - **Expected**: Status is `Pending Review` — over the auto-approve limit.

### 8.4 Payroll Processing (Finance)
1.  Navigate to **Finance** > **Payroll** or **Workforce** > **Payroll Reports**.
2.  Click **Generate Payroll** for `March 2026`.
    - System pulls attendance records, overtime, and leave deductions.
3.  Review the payroll slip for `Samantha Rivera`.
    - **Expected**: `Net Pay = Basic Salary + Overtime - Deductions - Tax`.
4.  Click **Process All**. Status: `Paid`.

### 8.5 Financial Summary Report (Finance)
1.  Navigate to **Finance** > **Dashboard** or **Financial Summary**.
2.  View the dashboard:
    - **Total Revenue**: Sum of all delivered Sales Orders.
    - **Total Expenses**: All approved expense requests + PO costs.
    - **Net Profit**: Revenue − Expenses.
    - **Expected**: All numbers should match the activity performed in earlier chapters.

### 8.6 RBAC Check on Finance (Production User)
1.  Log out. Log in as `production_user`. Try to visit **Finance** > **Payroll**.
    - **Expected**: Not visible or access denied. Finance data is restricted to Finance and Admin only.

---

## 🤖 Chapter 9: The Oracle Speaks — AI Assistant

The factory is running. The books are balanced. But there is one more force at play — **The Oracle**.

> Log in as `admin_user` to unlock the full AI toolset.

### 9.1 Inventory Intelligence
1.  Open the **AI Assistant** chat panel.
2.  **Ask**: *"What are the current stock levels for all raw materials?"*
    - **Expected**: AI lists `Pure Sugar: 1800 kg`, `Cola Concentrate: 150 L`.
3.  **Ask**: *"Which items are at risk of running out if we run one more batch of 1000 bottles?"*
    - **Expected**: AI calculates 200 kg sugar and 50 L concentrate needed, identifies no shortage.

### 9.2 Stock Movement via AI
1.  **Ask**: *"Transfer 500 kg of Pure Sugar from North Manufacturing Plant to South Distribution Center."*
    - **Expected**: AI asks for confirmation: *"Please confirm: Move 500 kg of Pure Sugar from North Manufacturing Plant to South Distribution Center?"*
2.  Type **"Yes, confirm"**.
    - **Expected**: Both warehouses reflect the transfer. A `StockMovement (ADJUST)` record is created.

### 9.3 Production Intelligence
1.  **Ask**: *"How many production batches were completed this month?"*
    - **Expected**: AI returns a count of completed orders.
2.  **Ask**: *"Which production line has the highest downtime?"*
    - **Expected**: AI queries MaintenanceTask records and returns hours lost per line.

### 9.4 Finance Intelligence
1.  **Ask**: *"What is the COGS (cost of goods sold) for the 750 bottles shipped to Mega Retail Group?"*
    - **Expected**: AI calculates: (750 × 0.2 kg × $0.50) + (750 × 0.05 L × $15.00) = $75 + $562.50 = **$637.50**.
2.  **Ask**: *"What is the net profit this month?"*
    - **Expected**: Revenue from delivered SOs minus all approved expenses.

### 9.5 AI RBAC Test
1.  Log out. Log in as `quality_user`.
2.  Open AI Assistant.
3.  **Ask**: *"Adjust the stock of Sugar by 1000 kg in North Plant."*
    - **Expected**: AI responds: *"Access Denied. You are logged in as Quality Controller. Stock adjustments require Admin or Store Manager role."*

---

## 📋 Epilogue: The Master Verification Matrix

| Module | Scenario | Action | Expected Outcome | RBAC Lock |
| :--- | :--- | :--- | :--- | :--- |
| **Inventory** | New Item | Admin creates item | Item in catalog, zero stock | Admin Only |
| **HR** | Hire & Clock-In | HR creates employee, checks in | Employee shows "Active" | HR / Admin |
| **HR** | Leave Request | HR submits & approves leave | Blocked dates on calendar | HR / Admin |
| **HR** | Training | Enroll employees | Profile shows "In Training" | HR / Admin |
| **Procurement** | Full PO Cycle | Create → Approve → Receive | Stock increments, status "Received" | Store / Admin |
| **Procurement** | Price List | Set vendor prices | Auto-fills on PO creation | Store / Admin |
| **Production** | Recipe | Alchemist creates formula | Ingredients linked to product | Production / Admin |
| **Production** | Batch | Schedule 1000 units | Ingredients reserved, batch on Gantt | Production / Admin |
| **Production** | Complete Batch | Click "Complete" | Raw items DOWN, finished goods UP | Production / Admin |
| **Maintenance** | Breakdown | Correct a machine | Health returns to 100% | Production / Admin |
| **Quality** | Test Pass | Submit pass test | Batch is "Approved", shippable | Quality / Admin |
| **Quality** | Test Fail | Submit fail test | Batch locked, un-shippable | Quality / Admin |
| **Sales** | Customer | Hustler creates customer | Profile in CRM | Sales / Admin |
| **Sales** | Sales Order | Create SO | Status pending, inventory reserved | Sales / Admin |
| **Sales** | Overstocking | Order > available stock | "Insufficient inventory" warning | Sales / Admin |
| **Logistics** | Dispatch | Start route | Vehicle status "In-Use" | Store / Admin |
| **Logistics** | Delivery | Confirm delivered | SO completed, inventory deducted | Store / Admin |
| **Finance** | Budget | Set department budget | Budget visible in expense queue | Finance / Admin |
| **Finance** | Auto-Approve | Small expense submit | Status instantly "Auto-Approved" | Any User |
| **Finance** | Payroll | Generate payroll | Net pay calculated correctly | Finance / Admin |
| **AI** | Inventory Query | Chat: "Show stock" | Lists all items with quantities | All Roles |
| **AI** | Stock Move | Chat: "Move X to Y" | Confirms, then updates both warehouses | Admin / Store |
| **AI** | COGS Analysis | Chat: "What is COGS?" | Calculated answer returned | Finance / Admin |
| **AI** | RBAC Block | Quality asks to adjust stock | AI returns "Unauthorized" | AI Enforced |

---

## 🚨 Troubleshooting Encyclopedia

| Symptom | Where to Look | Most Likely Cause |
| :--- | :--- | :--- |
| Can't log in | Browser Console > Network Tab | Wrong credentials or server not running |
| Stock didn't increase after PO receipt | Inventory > Stock Movements | GR was not confirmed |
| Batch won't start | Production > Equipment | Machine status is "Breakdown" |
| Batch can't be sold | Sales > Orders | Batch failed QC or QC not yet approved |
| Expense not auto-approved | Finance > Budget | Amount > auto-approve limit, or budget exhausted |
| AI returns "Unauthorized" | AI Chat | Logged-in user's role lacks required permission |
| Payroll numbers incorrect | Workforce > Attendance | Overtime / Deduction fields not populated |
| Stock moved but not showing | Inventory > Stock Movements | Check warehouse filters on the dashboard |

---

> 🏁 **The End of the First Day.** But in the life of FreshFizz ERP, this is just the beginning. Every action you took created a permanent, traceable record. Every permission boundary you tested is a wall that protects real data. Every AI query is a step toward an autonomous factory. **The story continues — run the next cycle.**
