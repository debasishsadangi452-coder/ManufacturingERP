# VGT Manufacturing ERP — Client MTO End-to-End Acceptance Test Script

**Purpose:** Verify the client workflow from confirmed customer order through production, procurement, quality, dispatch, invoicing, payment, and GL.

**Environment:** Local/staging only. Do not run transactional steps against production.

**Frontend:** http://localhost:8080  
**Backend:** http://localhost:8000

**Execution record**

- Tester:
- Date/time:
- Environment/build:
- Company:
- Test customer:
- Result: NOT RUN

## Rules for recording results

For each test, mark **PASS**, **FAIL**, or **BLOCKED**. A feature is not proven by a visible menu, database field, unit test, or successful page load alone: complete the action, verify its downstream effect, and save the stated evidence. Record every defect with the test ID, record IDs, expected/actual result, and screenshot or response.

Use clearly identifiable test records (prefix names/references with `E2E-`), and clean them up only after evidence has been retained and the environment owner approves.

## Test data to prepare

Create or select:

- One test customer and one supplier.
- One sellable finished good, one semi-finished good, two raw materials, and one packaging material.
- Base and alternate UOMs (for example, KG and Box) with a known conversion factor.
- A two-level BOM: raw materials → semi-finished good; semi-finished good + packaging → finished good.
- One recipe as a comparison only; when a BOM is active, expected planning/execution materials must come from the BOM.
- Raw-material, production, and finished-goods warehouses. Also identify whether this environment is configured for virtual or single-warehouse operation.
- At least two routing operations, a machine, manpower, shifts/capacity, and a QA inspection template.
- Opening stock deliberately set so one material is available and another has a known shortage.
- A test sales price, supplier purchase price, costing inputs, carrier, and transport mode.

Capture the starting stock, BOM quantities, UOM conversions, resource calendar, company settings, and test IDs before proceeding.

## Script — execute in order

### 0. Environment and tenant isolation

1. Sign in as an authorized test user and confirm the selected company.
2. Confirm the configured MTO/MTS strategy, warehouse model, business-rule sign-off state, default warehouses, currencies/tax settings, and accounting settings.
3. Confirm records created in this company are not visible to a different company/user without access.

**Expected:** The app and API are reachable; the selected company’s settings govern the workflow; no other tenant’s records are exposed.  
**Evidence:** Environment/build, company ID, settings screenshots, and any isolation test result.

### 1. Master data, UOM, warehouse, and resources

1. Create/open the item, customer, and supplier masters. Verify item codes, descriptions, classifications, default warehouses, and costing rules.
2. Create/open the two-level BOM and inspect each component, quantity, UOM, and effective/active status.
3. Convert a known quantity between base and alternate UOMs in both directions; test a non-integral result and invalid/zero conversion.
4. Inspect raw-material, production, and finished-goods warehouses. Repeat using the configured virtual/single-warehouse setup, if applicable.
5. Create/open machine and manpower resources, shifts, capacity, availability, and routing operations.

**Expected:** Raw, semi-finished, and finished item classes are distinct; BOM levels and conversions are correct; warehouse configuration supports the selected operating model; resources have usable availability and capacity.  
**Evidence:** Master IDs, BOM print/export, conversion calculations, warehouse configuration, and resource calendar.

### 2. Customer custom order and order book

1. Create an order directly as confirmed without creating a quotation. Repeat order entry for Email, Phone, and Direct/Manual sources (and any configured “Other” source).
2. Enter customer, item, quantity, promised delivery date, customer reference, priority, custom specifications, delivery requirements, and source.
3. Save and reopen the orders in the centralized order book.
4. Generate the customer-order confirmation document and compare it with the saved order.
5. Attempt invalid data (zero/negative quantity, missing required customer/item, invalid date) and an unauthorized action.

**Expected:** Each order is linked to its customer and item, preserves all entered values and source, has a unique reference/status, can bypass quotation in MTO mode, and is visible in the order book. Invalid/unauthorized actions are clearly rejected.  
**Evidence:** Order IDs, order-book row, confirmation document, and validation results.

### 3. Production plan and approvals

1. Generate a production plan from one confirmed customer order.
2. Generate a second plan from multiple confirmed orders if supported; confirm quantities and links stay attributable to each source order.
3. Verify product, planned quantity, delivery requirement, planned start/end, routing, and approval status.
4. Attempt plan creation from an unconfirmed order.
5. Attempt conversion while plan approval is required but missing; approve and retry.
6. Test partial plan conversion in two runs and verify cumulative converted quantity never exceeds planned quantity.

**Expected:** Plans originate from confirmed MTO orders, retain order lineage, schedule dates/resources, honor approval settings, and prevent overproduction.  
**Evidence:** Plan IDs, source-order links, schedule, approval audit, and conversion quantities.

### 4. BOM explosion, MRP, stock availability, and purchasing

1. Run planning/MRP for the finished-good quantity from the order.
2. Independently calculate expected component demand across both BOM levels, including packaging and UOM conversion.
3. Verify required, on-hand, allocated/reserved, open-purchase, net-needed, and shortage quantities for each component.
4. Change one stock balance and rerun MRP; verify the shortage changes by the expected converted quantity.
5. Create/trigger a purchase requisition or purchase order for the shortage.
6. Follow the procurement record back to the originating plan and customer order.
7. Verify supplier, quantity, UOM, delivery need, and remaining shortage after an open PO is considered.
8. Compare BOM-active and BOM-inactive/fallback behavior where the business configuration allows it.

**Expected:** Active BOM is the authoritative material source; multi-level demand and conversions are correct; availability and shortages account for stock and relevant open purchases; procurement is linked to the production/customer demand and does not silently over-order.  
**Evidence:** MRP report/record, independent calculation, requisition/PO IDs, and trace-back result.

### 5. Goods receipt and incoming raw-material QA

1. Receive the test PO in full; then receive another PO partially if partial receipts are supported.
2. Verify receipt quantity, warehouse, supplier/PO link, batch/lot, and inventory movement.
3. Run incoming QA and record a passing result for one material.
4. Record a failed or quarantined result for another material; attempt to allocate/issue it to production.
5. Record the configured rejection, supplier-return, or replacement path.

**Expected:** Receipt updates the correct stock and procurement balance; passed material becomes usable; failed/quarantined material cannot be consumed as approved stock; QA results and exception disposition remain traceable.  
**Evidence:** Goods receipt IDs, batch IDs, QA records, stock movements, and blocked-issue result.

### 6. Capacity validation and production-order conversion

1. Check planned load against machine and manpower capacity for the selected schedule.
2. Create a deliberately over-capacity plan and verify the configured warning/blocking behavior.
3. After required materials and approvals are available, convert the plan to a production order.
4. In MTO mode, attempt direct production-order creation without an eligible plan/customer order.
5. Verify production order quantity, schedule, routing, machine/manpower assignments, source plan, and customer-order link.

**Expected:** Capacity/utilization and shortages are visible before execution; conversion is gated by the configured rules; MTO direct-order bypass is prevented; created production order retains lineage.  
**Evidence:** Capacity result, conversion/audit records, production-order ID, and direct-create rejection.

### 7. Material issue, shop-floor operations, and WIP

1. Issue/reserve required raw and semi-finished materials to the production order, recording lot/batch and warehouse transfer as configured.
2. Start Operation 1; record operator/machine, start time, completed quantity, rejected/scrap quantity, and notes.
3. Move the order through each configured routing operation, including a deliberately delayed/incomplete operation.
4. Inspect the live WIP view by production order and by customer order; verify current operation/location, status, completed quantity, remaining quantity, and progress.
5. Attempt out-of-order operation completion, over-completion, and consumption of rejected/quarantined material.
6. Verify partial production and remainder status according to the configured business rules.

**Expected:** Operation sequence and quantities are enforced; WIP reflects actual recorded progress and bottlenecks; material consumption and production quantities reconcile; customer-order lookup identifies the corresponding WIP.  
**Evidence:** Issue/transfer records, operation logs, WIP screenshots, and rejected-action results.

### 8. In-process/final QA, rejection, scrap, and rework

1. Record an in-line QA pass and a failure; verify configured hold/rework behavior.
2. Complete production and attempt to transfer finished goods without required QA.
3. Record final QA pass for one lot and failure for another.
4. For the failed lot, execute the configured reject, scrap, or rework path; if reworked, repeat QA.
5. Verify rejected/quarantined stock is not available for allocation.

**Expected:** QA gates prevent unapproved finished stock from becoming available; pass/fail results, disposition, scrap/rework quantities, and repeat inspections are auditable.  
**Evidence:** QA IDs, batch statuses, inventory movement, rework/scrap records, and blocked-transfer result.

### 9. Product costing

1. Verify the configured costing formula and its sign-off state before interpreting the cost.
2. Run a cost calculation including actual/standard raw materials, operations, manpower, machine/resource use, and overhead.
3. Reconcile the calculated total and unit cost independently using the approved formula.
4. Change one known input (for example, one material price or machine hour) and verify the expected cost impact.
5. Confirm manufacturing cost is distinct from the sales price.

**Expected:** Cost components and allocation basis are visible and reproducible; changed inputs affect cost as expected; any unsigned formula is clearly identified as requiring client decision.  
**Evidence:** Cost breakdown, independent calculation, input-change result, and sign-off state.

### 10. Finished-goods receipt, reservation, picking, and dispatch

1. Complete production and passing QA; verify the finished quantity enters the configured FG warehouse.
2. Allocate/reserve finished quantity against its specific customer order.
3. Attempt allocation beyond available quantity and verify the response.
4. Generate a picking list; pick and stage the allocated quantity, recording warehouse/location and quantity.
5. Create dispatch with customer/order, quantity, carrier/vehicle, delivery information, and Air/Sea/Road mode.
6. Verify partial dispatch and remaining order balance if allowed.
7. Attempt dispatch of unpicked or unallocated quantity.

**Expected:** Only QA-approved FG is available; allocation is order-specific; picked, dispatched, and remaining quantities reconcile; invalid dispatches are blocked; shipment details are retained.  
**Evidence:** FG movement, allocation, pick list, dispatch/shipment IDs, and quantity reconciliation.

### 11. Documents

Generate, open, and compare the applicable printable/shareable outputs against the underlying records:

- Customer order confirmation.
- Production plan.
- Purchase order and goods receipt.
- Production order/work ticket/traveler and operational forms.
- Material requisition and stock-transfer voucher.
- QA record/clearance certificate.
- Dispatch pick list, delivery note, and dispatch note.
- Commercial invoice.

**Expected:** Each required document resolves to the correct transaction, customer/order, items, quantities, dates, status, and references; totals agree with source records; document output is usable for printing/sharing.  
**Evidence:** Saved PDF/print preview or screenshots for each document type.

### 12. Invoice, AR, payment, inventory accounting, and GL

1. Generate an invoice from a verified dispatch; verify invoice items and amounts match dispatched quantities and prices.
2. Confirm invoice appears in AR with the correct customer, due date, outstanding balance, and aging.
3. Record a partial payment and then settle the remaining balance; verify allocations and outstanding amounts.
4. Inspect accounting entries for the sales invoice, payment, inventory movements, material receipt, WIP/production, and FG where accounting is enabled.
5. Verify account mapping, dimensions, debit/credit balance, posting state, and source-document links.
6. Repeat a transaction with accounting integration disabled or an intentionally missing mapping only in the test environment; verify the configured warning/block and no misleading “posted” success.

**Expected:** Dispatch-to-invoice linkage is correct; AR and payment balances reconcile; enabled inventory/production/sales events post to configured accounts and dimensions; unmapped or failed postings are explicit.  
**Evidence:** Invoice, AR ledger, payment allocation, journal entries, reconciliation, and failure response.

### 13. End-to-end and reverse traceability

1. Open the original customer order and drill through plan → MRP/materials → requisition/PO → goods receipt/incoming QA → production order/operations/WIP → final QA → FG → allocation/picking → dispatch → invoice → AR/payment → GL.
2. Start reverse lookups from a production order, purchase order, goods receipt, finished-goods lot, dispatch, and invoice. Identify the originating customer order wherever one exists.
3. Verify each link uses actual record IDs and that partial production, receipts, and dispatches show the correct quantities and status.
4. Search by every supported identifier type and test unknown/invalid identifiers.

**Expected:** The chain is navigable in both directions where supported; no unrelated records are joined; each displayed status/quantity agrees with its source; unsupported searches are reported clearly.  
**Evidence:** Trace screenshots/export and a list of every traversed record ID.

### 14. Dashboards, BI, and order status

1. Verify the order book status and trace screen at each lifecycle milestone.
2. Verify production dashboard shows active orders, WIP stage/progress, delays, resource bottlenecks, and material shortages.
3. Verify material dashboard shows shortages/reorder indicators and FG available/reserved quantities.
4. Verify executive dashboard shows operational pipeline, fulfillment, financial metrics, and business-rule sign-off state.
5. Reconcile each displayed KPI to source records and record its definition/formula.

**Expected:** Dashboard values update after transactions, are scoped to the selected company, reconcile with source data, and surface unsigned/unfinalized KPI or business rules rather than presenting them as confirmed.  
**Evidence:** Dashboard screenshots, KPI definitions, and reconciliation worksheet.

### 15. Partial fulfilment and exception rules

Run one controlled case for each supported/approved rule:

1. Partial production followed by completion of the remainder.
2. Partial customer dispatch followed by a second dispatch.
3. Scrap and rejection during production.
4. Rework following a failed QA result.
5. Partial PO receipt and supplier replacement/return.
6. Short material with open PO, lead time, safety stock, reorder point, and MOQ configured.

**Expected:** Statuses and remaining quantities are consistent across order, plan, production, inventory, procurement, dispatch, invoice, and traceability. Do not mark a rule PASS if the client has not approved its behavior.  
**Evidence:** Before/after quantities, statuses, and the client-approved rule reference.

### 16. Business-rule and KPI sign-off

Review the following with the client and record the approved value/rule, approver, and date:

- RM QA trigger point and quarantine/release behavior.
- MRP treatment of lead time, safety stock, reorder point, MOQ, allocations, and open POs.
- Costing formula and overhead/resource allocation.
- Machine/manpower capacity basis, shifts, utilization, and standard vs actual hours.
- Scrap, rejection, rework, partial production, and partial dispatch.
- Approval requirements and status transitions.
- Exact dashboard KPI definitions for order status, WIP, material availability, FG availability, fulfillment, finance, and traceability.

**Expected:** No unresolved decision is silently treated as a settled business rule; configuration and dashboard flags show the outstanding items.  
**Evidence:** Client sign-off record and configuration screenshots.

## Requirement coverage checklist

Mark each requirement PASS only after the matching test above has passed and evidence is saved.

| Client requirement | Test section |
|---|---|
| 1. Make-to-Order as primary model | 0, 2, 3, 6 |
| 2. End-to-end linkage and traceability | 13 |
| 3. Configurable enterprise architecture | 0, 1, 16 |
| 4. Direct confirmed customer custom orders | 2 |
| 5. Multi-channel order sourcing | 2 |
| 6. Centralized customer order book | 2, 14 |
| 7. Multi-type item master | 1 |
| 8. Two-level BOM | 1, 4 |
| 9. Base/alternate UOM conversions | 1, 4 |
| 10. Machine and manpower resource master | 1 |
| 11. Production plans from confirmed orders | 3 |
| 12. Multi-level BOM explosion | 4 |
| 13. MRP and shortage calculation | 4 |
| 14. Purchase requisition/PO trigger and linkage | 4, 5 |
| 15. Resource and capacity planning | 1, 3, 6 |
| 16. Production-order conversion | 6 |
| 17. Sequential operation execution | 7 |
| 18. Granular WIP visibility | 7, 14 |
| 19. Customer-order-to-WIP lookup | 7, 13 |
| 20. Three-warehouse architecture | 1 |
| 21. Virtual/single-warehouse configuration | 1 |
| 22. Raw-material stock/allocation/shortage | 4, 5 |
| 23. Semi-finished inventory tracking | 1, 7 |
| 24. QA-approved FG inventory | 8, 10 |
| 25. Customer-order allocation/reservation | 10 |
| 26. Picking and packing | 10 |
| 27. Incoming raw-material QA | 5 |
| 28. In-line and finished-goods QA gates | 8 |
| 29. Dispatch fulfillment | 10 |
| 30. Transport mode and carrier tracking | 10 |
| 31. Dispatch-triggered invoicing | 12 |
| 32. AR, payments, balances, and aging | 12 |
| 33. GL integration and financial mapping | 12 |
| 34. Inventory accounting impact | 12 |
| 35. Multi-factor product costing | 9 |
| 36. Customer order confirmation | 2, 11 |
| 37. Production work ticket/traveler | 11 |
| 38. Delivery/dispatch notes | 11 |
| 39. Commercial sales invoice | 11, 12 |
| 40. Departmental transaction forms | 11 |
| 41. Executive/management dashboard | 14 |
| 42. Production manager dashboard | 14 |
| 43. Order status and traceability view | 13, 14 |
| 44. Material availability and inventory dashboard | 14 |
| 45. Integrated BI | 13, 14 |
| 46. RM QC trigger-point decision | 5, 16 |
| 47. Costing allocation decision | 9, 16 |
| 48. Capacity parameter decision | 1, 6, 16 |
| 49. MRP replenishment parameters | 4, 16 |
| 50. Scrap/rejection/rework/partial-flow decisions | 8, 15, 16 |

## Completion gate

The client workflow is accepted only when:

- Every applicable requirement above is PASS with evidence.
- Every FAIL has an owner, severity, and retest date.
- Every BLOCKED item has a named dependency/decision and approver.
- No unresolved P0/P1 issue remains.
- Order, inventory, production, procurement, shipment, invoice, AR, and GL quantities/amounts reconcile for the end-to-end test order.
- The client approves the open business rules and KPI definitions.
