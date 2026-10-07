"""KPI definitions (Track B, TB-20).

One list, used by every dashboard and published at
GET /api/insights/kpi-definitions/, so the formulas management reads are the
formulas the code computes. `dashboards` names where each KPI appears;
`status` is "agreed" (mechanical definition) or "provisional" (a business
choice awaiting client sign-off — see ERP_DOCS/TRACK_B_RULES.md).
"""

MANAGEMENT = "management"
PRODUCTION_MANAGER = "production_manager"
ORDER_STATUS = "order_status"
WIP = "wip"
MATERIAL = "material_availability"
FG = "fg_availability"
SUPPLY_CHAIN = "supply_chain"

DASHBOARDS = {
    MANAGEMENT: "Management Dashboard",
    PRODUCTION_MANAGER: "Production Manager Dashboard",
    ORDER_STATUS: "Order Status",
    WIP: "WIP Tracker",
    MATERIAL: "Material Availability",
    FG: "Finished Goods Availability",
    SUPPLY_CHAIN: "Supply Chain Visibility",
}


def _k(key, name, formula, scope, dashboards, status="agreed", source=""):
    return {"key": key, "name": name, "formula": formula, "scope": scope,
            "dashboards": dashboards, "status": status, "provisional": status == "provisional", "source": source}


KPI_DEFINITIONS = [
    # --- Orders ---------------------------------------------------------------
    _k("order_fulfilment_percent", "Order Fulfilment %",
       "Dispatched quantity / Confirmed order quantity x 100",
       "Order lines of confirmed, shipped and delivered orders created in the period.",
       [MANAGEMENT, ORDER_STATUS, SUPPLY_CHAIN], source="insights/executive.py:_orders"),
    _k("order_line_status", "Order line position",
       "Per line: ordered, allocated, picked, dispatched, invoiced, remaining = ordered - dispatched",
       "Every customer order line.", [ORDER_STATUS], source="fulfillment/services.py:order_fulfilment"),
    _k("order_status_rule", "Customer order status",
       "confirmed = released, nothing dispatched; shipped = partly dispatched; delivered = every line fully dispatched",
       "Partial production or QA never completes an order; only dispatch does.", [ORDER_STATUS, MANAGEMENT]),
    _k("delayed_order", "Delayed order / production",
       "Production order: planned end passed and not completed/closed. Customer order: has a delayed production order.",
       "Orders without a planned end are never counted as delayed.", [MANAGEMENT, PRODUCTION_MANAGER, ORDER_STATUS]),
    _k("order_book_value", "Order book value",
       "Sum of order totals of confirmed and partly shipped orders", "As of now.", [MANAGEMENT]),
    # --- Production / WIP -----------------------------------------------------
    _k("production_completion_percent", "Production Completion %",
       "Produced quantity / Planned production quantity x 100",
       "Production orders created in the period; cancelled and rework orders excluded.",
       [MANAGEMENT, PRODUCTION_MANAGER]),
    _k("wip_output_percent", "WIP output %",
       "Output reported / Planned quantity x 100", "Per production order.", [WIP, PRODUCTION_MANAGER],
       source="production/wip.py"),
    _k("wip_stage_percent", "WIP stage progress %",
       "Mean over operations of min((good + rejected + scrap + rework) / planned, 1) x 100",
       "Per production order.", [WIP, PRODUCTION_MANAGER], source="production/wip.py"),
    _k("wip_current_operation", "Current operation / location",
       "First operation in sequence that is not completed; its location (or the line's)",
       "Per production order.", [WIP, ORDER_STATUS]),
    _k("wip_bottleneck", "Bottleneck",
       "A paused / failed / rework-required operation; otherwise the open operation with the largest queue "
       "(units completed at the previous operation but not yet processed here)",
       "Per production order.", [WIP, PRODUCTION_MANAGER]),
    _k("wip_expected_completion", "Expected completion",
       "Planned end if scheduled; otherwise now + remaining operation minutes",
       "Per production order.", [WIP, ORDER_STATUS]),
    _k("wip_units", "WIP units", "Sum of remaining quantity of running and partially completed orders",
       "As of now.", [MANAGEMENT, PRODUCTION_MANAGER]),
    _k("wip_value", "WIP Value",
       "Cost of material issued to open production orders x (1 - produced / planned)",
       "Open production orders, material at standard unit cost.", [MANAGEMENT], status="provisional"),
    _k("qa_pass_rate_percent", "QA Pass Rate %", "QA accepted quantity / QA decided quantity x 100",
       "Production QA decisions made in the period.", [MANAGEMENT, PRODUCTION_MANAGER]),
    _k("machine_utilisation_percent", "Machine Utilisation %",
       "Machine load hours / Machine available hours x 100 (next 7 days)",
       "Active machines; see capacity formula (TRACK_B_RULES.md, TB-19).", [MANAGEMENT, PRODUCTION_MANAGER]),
    _k("manpower_utilisation_percent", "Manpower Utilisation %",
       "Manpower load hours / Manpower available hours x 100 (next 7 days)",
       "Active manpower resources.", [MANAGEMENT, PRODUCTION_MANAGER]),
    # --- Material -------------------------------------------------------------
    _k("material_availability_percent", "Material Availability %",
       "Material lines fully covered (issued + on hand >= required) / all material lines of open production orders x 100",
       "Material ledger of open production orders; open POs and safety stock not counted until TA-15 rules are agreed.",
       [MANAGEMENT, PRODUCTION_MANAGER, MATERIAL], status="provisional"),
    _k("material_shortage", "Material shortage",
       "required - issued - on hand, per open production order and material (> 0 only)",
       "Plus items at or below their reorder point.", [MATERIAL, PRODUCTION_MANAGER, MANAGEMENT]),
    _k("material_status", "Order material status",
       "available = remaining requirement on hand; partial = some issued, rest short; short = nothing issued and short",
       "Per production order.", [WIP, MATERIAL]),
    # --- Finished goods -------------------------------------------------------
    _k("fg_available", "FG Availability",
       "FG stock - quantity allocated to customer orders and not yet dispatched",
       "Excludes QA pending, QA failed, quarantine and rework, which never enter FG stock.",
       [MANAGEMENT, FG, ORDER_STATUS]),
    _k("fg_buckets", "FG by QA status",
       "produced; QA pending; QA approved; rejected; quarantine; rework; on hand; allocated; available; dispatched",
       "Per finished good.", [FG]),
    # --- Fulfilment / supply chain -------------------------------------------
    _k("on_time_delivery_percent", "On-time Dispatch %",
       "Dispatches confirmed on or before their expected delivery date / dispatches with an expected date x 100",
       "Dispatches confirmed in the period.", [MANAGEMENT, SUPPLY_CHAIN], status="provisional"),
    _k("pipeline", "Order pipeline",
       "Units ordered → planned → produced → QA approved → allocated → dispatched → invoiced",
       "Confirmed orders created in the period.", [MANAGEMENT, SUPPLY_CHAIN]),
    _k("traceability_stage_complete", "Traceability stage reached",
       "A stage is reached when at least one linked record exists for it",
       "17 stages from customer order to GL.", [SUPPLY_CHAIN, ORDER_STATUS], source="insights/traceability.py"),
    # --- Finance --------------------------------------------------------------
    _k("ar_outstanding", "AR Outstanding",
       "Sum of (invoice total - amount paid) over open and partially paid invoices",
       "As of now; cancelled invoices excluded.", [MANAGEMENT]),
    _k("ar_overdue", "AR Overdue", "AR outstanding on invoices past their due date", "As of now.", [MANAGEMENT]),
    _k("collections", "Collections", "Sum of customer payments received in the period", "Payment date in period.", [MANAGEMENT]),
    _k("revenue", "Revenue", "Sum of invoice totals dated in the period", "Cancelled excluded.", [MANAGEMENT]),
]


def definitions(dashboard=None):
    if dashboard:
        return [k for k in KPI_DEFINITIONS if dashboard in k["dashboards"]]
    return KPI_DEFINITIONS


def percent(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0
