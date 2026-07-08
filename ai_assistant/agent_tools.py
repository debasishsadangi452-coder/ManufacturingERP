"""
Advanced analytics & prediction tools that power the named AI agents
(AI Plant Manager, AI Procurement, AI Finance, AI Maintenance,
AI Production Planner, AI Sales Assistant, AI Quality).

These follow the same conventions as tools.py: every tool takes the
requesting user as its first argument and returns a JSON string.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum, Max, Count, Avg, Q
from django.utils import timezone

from inventory.models import Stock, Item
from sales.models import SalesOrder, Customer
from procurement.models import PurchaseOrder, Vendor, VendorPriceList
from production.models import ProductionOrder, ProductionLine, Recipe, RecipeIngredient
from maintenance.models import Equipment, MaintenanceTask
from quality.models import QualityCheck
from finance.models import ExpenseRequest, FinancialSummary, OperationalCost
from workforce.models import Employee, AttendanceRecord


FAILURE_HEALTH_THRESHOLD = 40  # below this, equipment is considered at failure risk


# ---------------------------------------------------------------------------
# AI MAINTENANCE — predict machine failure instead of preventive schedules
# ---------------------------------------------------------------------------

def predict_equipment_failure(user):
    """Predict which machines are likely to fail and in how many days,
    based on health degradation since their last maintenance."""
    if user.role not in ['admin', 'production', 'quality']:
        return json.dumps({"error": "Unauthorized access to maintenance data."})

    today = timezone.localdate()
    predictions = []
    for eq in Equipment.objects.filter(line__company=user.company).select_related('line'):
        health = eq.health if eq.health is not None else 100
        if eq.last_maintenance:
            days_since = max((today - eq.last_maintenance).days, 1)
        else:
            days_since = 30  # no record — assume the health deficit built up over a month

        degradation_per_day = max((100 - health) / days_since, 0.05)
        margin = health - FAILURE_HEALTH_THRESHOLD
        days_to_failure = int(margin / degradation_per_day) if margin > 0 else 0

        if days_to_failure <= 0:
            risk = "critical"
        elif days_to_failure <= 14:
            risk = "high"
        elif days_to_failure <= 30:
            risk = "medium"
        else:
            risk = "low"

        predictions.append({
            "equipment_id": eq.id,
            "equipment": eq.name,
            "line": eq.line.name if eq.line else "Unassigned",
            "status": eq.status,
            "health": health,
            "uptime": eq.uptime,
            "days_since_last_maintenance": days_since if eq.last_maintenance else None,
            "predicted_days_to_failure": days_to_failure,
            "risk": risk,
        })

    predictions.sort(key=lambda p: p["predicted_days_to_failure"])
    at_risk = [p for p in predictions if p["risk"] in ("critical", "high")]
    return json.dumps({
        "predictions": predictions[:15],
        "at_risk_count": len(at_risk),
        "recommendation": (
            "Schedule maintenance now for critical/high risk machines using schedule_maintenance."
            if at_risk else "No machines at imminent risk. No action needed."
        ),
    })


# ---------------------------------------------------------------------------
# AI PROCUREMENT — detect reorder needs and recommend suppliers
# ---------------------------------------------------------------------------

def detect_reorder_needs(user, threshold=100):
    """Find items whose total stock is below the reorder threshold and
    recommend the best supplier for each, ready for one-click PO creation."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized access to procurement data."})

    low = (
        Stock.objects.filter(item__company=user.company).values('item_id', 'item__name', 'item__unit')
        .annotate(total=Sum('quantity'))
        .filter(total__lt=threshold)
        .order_by('total')
    )

    needs = []
    for row in low[:15]:
        suggested_qty = max(round(threshold * 2 - (row['total'] or 0)), 1)
        best = (
            VendorPriceList.objects.filter(item_id=row['item_id'], is_active=True, vendor__company=user.company)
            .select_related('vendor')
            .order_by('unit_price', 'lead_time_days')
            .first()
        )
        needs.append({
            "item_id": row['item_id'],
            "item": row['item__name'],
            "current_stock": row['total'],
            "unit": row['item__unit'],
            "suggested_order_qty": suggested_qty,
            "recommended_vendor": (
                {
                    "vendor_id": best.vendor.id,
                    "vendor": best.vendor.name,
                    "unit_price": str(best.unit_price),
                    "lead_time_days": best.lead_time_days,
                    "rating": best.vendor.rating,
                }
                if best else None
            ),
        })

    return json.dumps({
        "reorder_needs": needs,
        "note": (
            "To place an order, confirm with the user then call create_purchase_order "
            "with the recommended vendor_id and item quantities."
        ),
    })


def recommend_suppliers(user, item_name):
    """Rank suppliers for an item by price, lead time and vendor rating."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized access to procurement data."})

    item = Item.objects.filter(name__icontains=item_name, company=user.company).first()
    if not item:
        return json.dumps({"error": f"No item matching '{item_name}' found."})

    entries = (
        VendorPriceList.objects.filter(item=item, is_active=True, vendor__company=user.company)
        .select_related('vendor')
    )
    if not entries.exists():
        return json.dumps({
            "item": item.name,
            "suppliers": [],
            "note": "No vendor price list entries for this item. Ask the user to add vendor prices first.",
        })

    max_price = max(float(e.unit_price) for e in entries) or 1.0
    max_lead = max(e.lead_time_days for e in entries) or 1

    ranked = []
    for e in entries:
        # Composite score: cheaper, faster and better-rated vendors score higher
        price_score = 1 - (float(e.unit_price) / max_price)
        lead_score = 1 - (e.lead_time_days / max_lead)
        rating_score = (e.vendor.rating or 0) / 5.0
        score = round(0.5 * price_score + 0.3 * lead_score + 0.2 * rating_score, 3)
        ranked.append({
            "vendor_id": e.vendor.id,
            "vendor": e.vendor.name,
            "unit_price": str(e.unit_price),
            "currency": e.currency,
            "lead_time_days": e.lead_time_days,
            "min_order_qty": e.min_order_qty,
            "rating": e.vendor.rating,
            "score": score,
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)

    return json.dumps({
        "item_id": item.id,
        "item": item.name,
        "suppliers": ranked,
        "best_pick": ranked[0]["vendor"] if ranked else None,
    })


# ---------------------------------------------------------------------------
# AI FINANCE — cash flow forecasting
# ---------------------------------------------------------------------------

def forecast_cash_flow(user, horizon_days=30):
    """Forecast cash inflows and outflows over the next N days from open
    sales orders, open purchase orders, pending expenses and payroll history."""
    if user.role not in ['admin', 'finance']:
        return json.dumps({"error": "Unauthorized access to finance data."})

    horizon_days = int(horizon_days or 30)

    expected_inflow = SalesOrder.objects.filter(customer__company=user.company,
        status__in=['pending', 'confirmed', 'shipped']
    ).aggregate(t=Sum('total_amount'))['t'] or Decimal("0")

    open_po_outflow = PurchaseOrder.objects.filter(vendor__company=user.company,
        status__in=['pending', 'approved', 'ordered']
    ).aggregate(t=Sum('total_amount'))['t'] or Decimal("0")

    pending_expenses = ExpenseRequest.objects.filter(company=user.company,
        status='pending'
    ).aggregate(t=Sum('amount'))['t'] or Decimal("0")

    # Historical monthly averages from the closed books
    history = FinancialSummary.objects.filter(company=user.company).order_by('-created_at')[:3]
    if history:
        avg_revenue = sum(h.total_revenue for h in history) / len(history)
        avg_expenses = sum(h.total_expenses for h in history) / len(history)
        avg_payroll = sum(h.total_payroll for h in history) / len(history)
    else:
        avg_revenue = avg_expenses = avg_payroll = Decimal("0")

    scale = Decimal(horizon_days) / Decimal(30)
    projected_inflow = expected_inflow + (avg_revenue * scale)
    projected_outflow = open_po_outflow + pending_expenses + ((avg_expenses + avg_payroll) * scale)
    net = projected_inflow - projected_outflow

    return json.dumps({
        "horizon_days": horizon_days,
        "expected_inflows": {
            "open_sales_orders": str(expected_inflow),
            "projected_recurring_revenue": str(round(avg_revenue * scale, 2)),
        },
        "expected_outflows": {
            "open_purchase_orders": str(open_po_outflow),
            "pending_expense_requests": str(pending_expenses),
            "projected_operating_and_payroll": str(round((avg_expenses + avg_payroll) * scale, 2)),
        },
        "projected_net_cash_flow": str(round(net, 2)),
        "outlook": "positive" if net >= 0 else "negative",
        "based_on": f"{len(history)} closed financial period(s) plus current open orders",
    })


# ---------------------------------------------------------------------------
# AI PRODUCTION PLANNER — order feasibility (material + capacity + manpower)
# ---------------------------------------------------------------------------

def check_order_feasibility(user, product_name, quantity, due_date=None, shift_hours_per_day=8):
    """Answer 'can we finish this order by <date>?' by checking raw materials,
    line capacity and manpower, and suggesting overtime if it falls short."""
    if user.role not in ['admin', 'production', 'sales']:
        return json.dumps({"error": "Unauthorized access to production planning."})

    quantity = float(quantity)
    recipe = (
        Recipe.objects.filter(product__name__icontains=product_name, product__company=user.company)
        .select_related('product')
        .first()
    )
    if not recipe:
        return json.dumps({"error": f"No recipe found for product '{product_name}'."})

    # 1. Raw materials
    shortages = []
    for ing in RecipeIngredient.objects.filter(recipe=recipe).select_related('item'):
        required = ing.quantity * quantity
        available = Stock.objects.filter(item=ing.item).aggregate(t=Sum('quantity'))['t'] or 0
        if available < required:
            shortages.append({
                "item": ing.item.name,
                "required": required,
                "available": available,
                "shortfall": round(required - available, 2),
            })

    # 2. Machine / line capacity
    lines = ProductionLine.objects.filter(is_active=True, status='running', company=user.company)
    total_capacity_per_hour = sum(l.capacity for l in lines) or 0

    if due_date:
        try:
            from datetime import date
            due = date.fromisoformat(str(due_date))
            days_available = max((due - timezone.localdate()).days, 0)
        except ValueError:
            return json.dumps({"error": f"Invalid due_date '{due_date}'. Use YYYY-MM-DD."})
    else:
        days_available = 7

    hours_needed = (quantity / total_capacity_per_hour) if total_capacity_per_hour else None
    hours_available = days_available * shift_hours_per_day

    # 3. Manpower on the floor
    workforce_count = Employee.objects.filter(company=user.company,
        status='active'
    ).filter(
        Q(department__name__icontains='produc') | Q(assigned_line__gt='')
    ).count()

    feasible_materials = not shortages
    feasible_capacity = hours_needed is not None and hours_needed <= hours_available
    overtime_hours = (
        round(hours_needed - hours_available, 1)
        if hours_needed is not None and hours_needed > hours_available else 0
    )
    can_do_with_overtime = (
        hours_needed is not None
        and overtime_hours > 0
        and hours_needed <= days_available * (shift_hours_per_day + 4)
    )

    if feasible_materials and feasible_capacity:
        verdict = "YES — order is feasible by the due date."
    elif feasible_materials and can_do_with_overtime:
        verdict = f"YES, WITH OVERTIME — needs about {overtime_hours} extra machine-hours."
    else:
        verdict = "NO — order is not feasible as things stand."

    return json.dumps({
        "product": recipe.product.name,
        "quantity": quantity,
        "due_in_days": days_available,
        "verdict": verdict,
        "materials": {"sufficient": feasible_materials, "shortages": shortages},
        "capacity": {
            "active_lines": lines.count(),
            "total_units_per_hour": total_capacity_per_hour,
            "hours_needed": round(hours_needed, 1) if hours_needed is not None else "unknown (no active lines)",
            "hours_available": hours_available,
            "overtime_hours_required": overtime_hours,
        },
        "manpower": {"production_staff_active": workforce_count},
        "next_steps": (
            "If materials are short, hand over to AI Procurement to reorder. "
            "To lock it in, create a production order with create_production_order."
        ),
    })


# ---------------------------------------------------------------------------
# AI QUALITY — rejection hotspots and defect trends
# ---------------------------------------------------------------------------

def analyze_quality_performance(user, days=30):
    """Find the highest-rejection production line, top defect types and
    whether quality is trending better or worse over the last N days."""
    if user.role not in ['admin', 'quality', 'production']:
        return json.dumps({"error": "Unauthorized access to quality data."})

    days = int(days or 30)
    since = timezone.now() - timedelta(days=days)
    checks = QualityCheck.objects.filter(inspected_at__gte=since, production_order__recipe__product__company=user.company).select_related(
        'production_order__line'
    )
    total = checks.count()
    if total == 0:
        return json.dumps({"summary": f"No quality checks recorded in the last {days} days."})

    rejected = checks.filter(status='rejected')

    # Rejections by production line
    by_line = {}
    for c in checks:
        line = c.production_order.line.name if c.production_order and c.production_order.line else "Unassigned"
        stats = by_line.setdefault(line, {"checks": 0, "rejected": 0})
        stats["checks"] += 1
        if c.status == 'rejected':
            stats["rejected"] += 1
    line_rates = [
        {
            "line": line,
            "checks": s["checks"],
            "rejected": s["rejected"],
            "rejection_rate_pct": round(100 * s["rejected"] / s["checks"], 1),
        }
        for line, s in by_line.items()
    ]
    line_rates.sort(key=lambda r: r["rejection_rate_pct"], reverse=True)

    # Top defect types
    defects = (
        rejected.exclude(test_type='')
        .values('test_type')
        .annotate(count=Count('id'))
        .order_by('-count')[:5]
    )

    # Trend: first half vs second half of the window
    midpoint = timezone.now() - timedelta(days=days / 2)
    older = checks.filter(inspected_at__lt=midpoint)
    recent = checks.filter(inspected_at__gte=midpoint)

    def _rate(qs):
        n = qs.count()
        return round(100 * qs.filter(status='rejected').count() / n, 1) if n else 0.0

    older_rate, recent_rate = _rate(older), _rate(recent)

    return json.dumps({
        "window_days": days,
        "total_checks": total,
        "total_rejected": rejected.count(),
        "overall_rejection_rate_pct": round(100 * rejected.count() / total, 1),
        "highest_rejection_line": line_rates[0] if line_rates else None,
        "rejection_by_line": line_rates,
        "top_defect_types": list(defects),
        "trend": {
            "earlier_half_rate_pct": older_rate,
            "recent_half_rate_pct": recent_rate,
            "direction": "worsening" if recent_rate > older_rate else "improving" if recent_rate < older_rate else "flat",
        },
    })


# ---------------------------------------------------------------------------
# AI SALES ASSISTANT — dormant customers
# ---------------------------------------------------------------------------

def find_dormant_customers(user, days=90):
    """List customers who have not placed an order in the last N days."""
    if user.role not in ['admin', 'sales']:
        return json.dumps({"error": "Unauthorized access to sales data."})

    days = int(days or 90)
    cutoff = timezone.now() - timedelta(days=days)

    customers = Customer.objects.filter(company=user.company).annotate(
        last_order=Max('salesorder__created_at'),
        lifetime_value=Sum('salesorder__total_amount'),
    )
    dormant = []
    for c in customers:
        if c.last_order is None or c.last_order < cutoff:
            dormant.append({
                "customer_id": c.id,
                "customer": c.name,
                "email": c.email,
                "phone": c.phone,
                "last_order": c.last_order.date().isoformat() if c.last_order else "never ordered",
                "days_inactive": (timezone.now() - c.last_order).days if c.last_order else None,
                "lifetime_value": str(c.lifetime_value or 0),
            })

    dormant.sort(key=lambda d: float(d["lifetime_value"]), reverse=True)
    return json.dumps({
        "inactive_threshold_days": days,
        "dormant_customer_count": len(dormant),
        "dormant_customers": dormant[:20],
        "suggestion": "Prioritise win-back outreach by lifetime value (highest first).",
    })


# ---------------------------------------------------------------------------
# AI PLANT MANAGER — which line is losing money?
# ---------------------------------------------------------------------------

def analyze_line_profitability(user, days=30):
    """Per production line: output, rejections, maintenance cost and downtime —
    highlighting the line with the worst cost per unit produced."""
    if user.role not in ['admin', 'production', 'finance']:
        return json.dumps({"error": "Unauthorized access to plant analytics."})

    days = int(days or 30)
    since = timezone.now() - timedelta(days=days)
    report = []

    for line in ProductionLine.objects.filter(company=user.company):
        orders = ProductionOrder.objects.filter(line=line, created_at__gte=since)
        completed_units = orders.filter(status='completed').aggregate(t=Sum('quantity'))['t'] or 0
        delayed = orders.filter(status='delayed').count()

        line_checks = QualityCheck.objects.filter(
            production_order__line=line, inspected_at__gte=since
        )
        rejected = line_checks.filter(status='rejected').count()

        equipment = Equipment.objects.filter(line=line)
        avg_health = equipment.aggregate(a=Avg('health'))['a'] or 100
        # MaintenanceTask has no created timestamp, so cost is all-time for the line
        maintenance_cost = MaintenanceTask.objects.filter(
            equipment__line=line
        ).aggregate(t=Sum('amount'))['t'] or Decimal("0")

        cost_per_unit = (
            round(float(maintenance_cost) / completed_units, 2)
            if completed_units else None
        )
        report.append({
            "line": line.name,
            "status": line.status,
            "units_completed": completed_units,
            "delayed_orders": delayed,
            "quality_rejections": rejected,
            "avg_equipment_health": round(avg_health, 1),
            "maintenance_cost": str(maintenance_cost),
            "maintenance_cost_per_unit": cost_per_unit,
        })

    # The "losing money" line: highest cost per unit (lines with no output rank worst)
    def _pain(r):
        if r["units_completed"] == 0:
            return float('inf')
        return (r["maintenance_cost_per_unit"] or 0) + r["quality_rejections"]

    report.sort(key=_pain, reverse=True)
    return json.dumps({
        "window_days": days,
        "worst_performing_line": report[0]["line"] if report else None,
        "lines": report,
        "how_to_read": (
            "The worst line combines high maintenance cost per unit, rejections and zero/low output. "
            "Lines with zero completed units but ongoing costs are pure loss-makers."
        ),
    })


# ---------------------------------------------------------------------------
# DIGITAL TWIN — one snapshot of the whole factory
# ---------------------------------------------------------------------------

def get_digital_twin_snapshot(user):
    """One-screen factory snapshot: sales, profit, production, inventory,
    machine health, procurement, attendance and open customer orders."""
    today = timezone.localdate()
    month_ago = timezone.now() - timedelta(days=30)

    revenue_30d = SalesOrder.objects.filter(created_at__gte=month_ago, customer__company=user.company).exclude(
        status='cancelled'
    ).aggregate(t=Sum('total_amount'))['t'] or 0
    open_orders = SalesOrder.objects.filter(status__in=['pending', 'confirmed'], customer__company=user.company).count()

    latest_summary = FinancialSummary.objects.filter(company=user.company).order_by('-created_at').first()

    prod_counts = dict(
        ProductionOrder.objects.filter(recipe__product__company=user.company).values_list('status').annotate(c=Count('id'))
    )
    lines_running = ProductionLine.objects.filter(is_active=True, status='running', company=user.company).count()
    lines_total = ProductionLine.objects.filter(company=user.company).count()

    total_items = Item.objects.filter(company=user.company).count()
    low_stock = (
        Stock.objects.filter(item__company=user.company).values('item_id').annotate(t=Sum('quantity')).filter(t__lt=100).count()
    )

    avg_health = Equipment.objects.filter(line__company=user.company).aggregate(a=Avg('health'))['a']
    critical_machines = Equipment.objects.filter(health__lt=FAILURE_HEALTH_THRESHOLD + 20, line__company=user.company).count()

    open_pos = PurchaseOrder.objects.filter(status__in=['pending', 'approved', 'ordered'], vendor__company=user.company)
    open_po_value = open_pos.aggregate(t=Sum('total_amount'))['t'] or 0

    present = AttendanceRecord.objects.filter(date=today, status='present', employee__company=user.company).count()
    active_employees = Employee.objects.filter(status='active', company=user.company).count()

    return {
        "as_of": timezone.now().isoformat(),
        "sales": {"revenue_last_30d": str(revenue_30d), "open_orders": open_orders},
        "profit": {
            "period": latest_summary.period_label if latest_summary else None,
            "net_profit": str(latest_summary.net_profit) if latest_summary else None,
            "revenue": str(latest_summary.total_revenue) if latest_summary else None,
            "expenses": str(latest_summary.total_expenses) if latest_summary else None,
        },
        "production": {
            "running": prod_counts.get('running', 0),
            "scheduled": prod_counts.get('scheduled', 0),
            "delayed": prod_counts.get('delayed', 0),
            "completed": prod_counts.get('completed', 0),
            "lines_running": lines_running,
            "lines_total": lines_total,
        },
        "inventory": {"total_items": total_items, "low_stock_items": low_stock},
        "machine_health": {
            "average_health": round(avg_health, 1) if avg_health is not None else None,
            "machines_at_risk": critical_machines,
            "total_machines": Equipment.objects.filter(line__company=user.company).count(),
        },
        "procurement": {"open_purchase_orders": open_pos.count(), "open_po_value": str(open_po_value)},
        "workforce": {"present_today": present, "active_employees": active_employees},
    }


def get_digital_twin(user):
    """Tool wrapper for agents: JSON snapshot of the whole factory."""
    return json.dumps(get_digital_twin_snapshot(user))


# ---------------------------------------------------------------------------
# Registry: map + Groq tool definitions for the new tools
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Uniform, template-driven procurement (deterministic — no LLM composition).
# The tool builds a fixed-format message; the agent must relay it verbatim.
# ---------------------------------------------------------------------------

def procure_item(user, item_name, quantity):
    """Uniform procurement: check item -> coupon price -> vendor. If all
    present, raise the PO and prompt to receive. Otherwise, ask the user to
    add the missing vendor/coupon price. Returns a fixed template string."""
    if user.role not in ('admin', 'store'):
        return json.dumps({"template": "PROCUREMENT · You are not authorized to raise purchase orders."})

    company = user.company
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return json.dumps({"template": "PROCUREMENT · Please specify a quantity greater than zero."})

    item = Item.objects.filter(name__icontains=item_name, company=company).first()
    if not item:
        return json.dumps({"template": (
            f"PROCUREMENT · Item not found\n"
            f"No item matching '{item_name}' exists for {company}.\n"
            f"Add the item first, then order it."
        )})

    # Best coupon price = cheapest active vendor price for this item
    price = (
        VendorPriceList.objects.filter(item=item, is_active=True, vendor__company=company)
        .select_related("vendor").order_by("unit_price").first()
    )

    if not price:
        return json.dumps({
            "template": (
                f"PROCUREMENT · Missing vendor / coupon price\n"
                f"Item: {item.name}\n"
                f"Quantity: {qty:g} {item.unit}\n"
                f"No vendor or coupon price is on file for this item.\n"
                f"Fill in the vendor and coupon price below to order it."
            ),
            # Drives an inline fill-in form in the chat (submitted without the LLM)
            "form": {
                "kind": "vendor_price",
                "item": item.name,
                "unit": item.unit,
                "quantity": qty,
            },
        })

    vendor = price.vendor
    unit_price = Decimal(str(price.unit_price))
    total = unit_price * Decimal(str(qty))

    po = PurchaseOrder.objects.create(vendor=vendor, status="approved")
    from procurement.models import PurchaseOrderItem
    PurchaseOrderItem.objects.create(
        purchase_order=po, item=item, quantity=qty, unit_price=unit_price
    )
    po.refresh_from_db()

    moq_note = ""
    if qty < (price.min_order_qty or 0):
        moq_note = f"\nNote: vendor minimum order qty is {price.min_order_qty:g} {item.unit}."

    return json.dumps({"template": (
        f"PURCHASE ORDER PREPARED · PO-{po.id:04d}\n"
        f"Item        : {item.name}\n"
        f"Quantity    : {qty:g} {item.unit}\n"
        f"Vendor      : {vendor.name} (rating {vendor.rating or 0}/5)\n"
        f"Coupon price: {price.currency} {unit_price:g} / {item.unit}\n"
        f"Lead time   : {price.lead_time_days} days\n"
        f"Total       : {price.currency} {total:g}\n"
        f"Status      : Approved{moq_note}\n\n"
        f'Reply "receive PO {po.id}" to book the goods into inventory.'
    )})


def receive_procurement(user, po_id, warehouse_name=None):
    """Receive an approved/ordered PO: book stock in and record the cost.
    Returns a fixed template string."""
    if user.role not in ('admin', 'store'):
        return json.dumps({"template": "PROCUREMENT · You are not authorized to receive goods."})

    company = user.company
    from inventory.models import Warehouse
    from inventory.services import increase_stock
    from procurement.models import GoodsReceipt

    po = PurchaseOrder.objects.filter(id=po_id, vendor__company=company).first()
    if not po:
        return json.dumps({"template": f"PROCUREMENT · PO {po_id} not found for {company}."})
    if po.status not in ("approved", "ordered"):
        return json.dumps({"template": (
            f"PROCUREMENT · PO-{po.id:04d} is '{po.status}'. "
            f"Only Approved or Ordered POs can be received."
        )})

    if warehouse_name:
        warehouse = Warehouse.objects.filter(name__icontains=warehouse_name, company=company).first()
    else:
        warehouse = Warehouse.objects.filter(company=company).first()
    if not warehouse:
        return json.dumps({"template": "PROCUREMENT · No warehouse configured for this company."})

    receipt = GoodsReceipt.objects.create(purchase_order=po, warehouse=warehouse)
    lines = []
    for poi in po.items.all():
        increase_stock(poi.item, warehouse, poi.quantity, user=user, reference=f"GRN PO#{po.id} (AI)")
        lines.append(f"  {poi.quantity:g} {poi.item.unit} {poi.item.name}")
    po.status = "received"
    po.save()
    from finance.services import record_procurement_cost
    record_procurement_cost(po, user=user)

    return json.dumps({"template": (
        f"GOODS RECEIVED · PO-{po.id:04d}\n"
        f"Warehouse: {warehouse.name}\n"
        f"Booked into inventory:\n" + "\n".join(lines) + "\n"
        f"Cost recorded in Finance: {po.total_amount:g}"
    )})


def add_vendor_price(user, vendor_name, item_name, unit_price, lead_time_days=7):
    """Create (or reuse) a vendor and set its coupon price for an item.
    Used when procure_item reports a missing vendor/price. Templated output."""
    if user.role not in ('admin', 'store'):
        return json.dumps({"template": "PROCUREMENT · You are not authorized to add vendors."})

    company = user.company
    item = Item.objects.filter(name__icontains=item_name, company=company).first()
    if not item:
        return json.dumps({"template": f"PROCUREMENT · Item '{item_name}' not found for {company}."})

    vendor = Vendor.objects.filter(name__iexact=vendor_name, company=company).first()
    if not vendor:
        vendor = Vendor.objects.create(name=vendor_name, company=company)

    try:
        price = Decimal(str(unit_price))
    except Exception:
        return json.dumps({"template": "PROCUREMENT · Coupon price must be a number."})

    VendorPriceList.objects.update_or_create(
        vendor=vendor, item=item,
        defaults={"unit_price": price, "lead_time_days": int(lead_time_days or 7), "is_active": True},
    )
    return json.dumps({"template": (
        f"VENDOR & COUPON PRICE SAVED\n"
        f"Vendor      : {vendor.name}\n"
        f"Item        : {item.name}\n"
        f"Coupon price: {price:g} / {item.unit}\n"
        f"Lead time   : {int(lead_time_days or 7)} days\n\n"
        f'You can now say "order <qty> {item.name}".'
    )})


AGENT_TOOL_MAP = {
    "procure_item": procure_item,
    "receive_procurement": receive_procurement,
    "add_vendor_price": add_vendor_price,
    "predict_equipment_failure": predict_equipment_failure,
    "detect_reorder_needs": detect_reorder_needs,
    "recommend_suppliers": recommend_suppliers,
    "forecast_cash_flow": forecast_cash_flow,
    "check_order_feasibility": check_order_feasibility,
    "analyze_quality_performance": analyze_quality_performance,
    "find_dormant_customers": find_dormant_customers,
    "analyze_line_profitability": analyze_line_profitability,
    "get_digital_twin": get_digital_twin,
}

AGENT_TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "procure_item",
            "description": "Uniform procurement for one item. Checks the item, its coupon (vendor) price and vendor; if all present it raises the PO and returns a fixed template asking the user to receive it. If the vendor/coupon price is missing it returns a template asking the user to add them. ALWAYS relay the returned 'template' text verbatim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "Name of the item to order"},
                    "quantity": {"type": "number", "description": "Quantity to order"},
                },
                "required": ["item_name", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "receive_procurement",
            "description": "Receive an approved/ordered purchase order: books stock into inventory and records the cost in finance. Relay the returned 'template' text verbatim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "po_id": {"type": "integer", "description": "Purchase order id to receive"},
                    "warehouse_name": {"type": "string", "description": "Optional destination warehouse name"},
                },
                "required": ["po_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_vendor_price",
            "description": "Add (or update) a vendor and its coupon price for an item. Use when procure_item reports a missing vendor/coupon price. Relay the returned 'template' text verbatim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "item_name": {"type": "string"},
                    "unit_price": {"type": "number", "description": "Coupon price per unit"},
                    "lead_time_days": {"type": "integer", "description": "Vendor lead time in days (default 7)"},
                },
                "required": ["vendor_name", "item_name", "unit_price"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "predict_equipment_failure",
            "description": "Predict which machines are likely to fail and in how many days, ranked by risk.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_reorder_needs",
            "description": "Find items below the reorder threshold with a recommended supplier for each, ready for one-click purchase orders.",
            "parameters": {
                "type": "object",
                "properties": {"threshold": {"type": "number", "description": "Stock level below which to reorder (default 100)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_suppliers",
            "description": "Rank suppliers for a given item by price, lead time and rating.",
            "parameters": {
                "type": "object",
                "properties": {"item_name": {"type": "string"}},
                "required": ["item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forecast_cash_flow",
            "description": "Forecast cash inflows, outflows and net cash flow over the next N days.",
            "parameters": {
                "type": "object",
                "properties": {"horizon_days": {"type": "integer", "description": "Forecast horizon in days (default 30)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_order_feasibility",
            "description": "Answer whether an order of N units of a product can be finished by a due date, checking materials, line capacity, manpower and overtime.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "quantity": {"type": "number"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["product_name", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_quality_performance",
            "description": "Rejection rate by production line, top defect types and quality trend over the last N days.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Analysis window in days (default 30)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_dormant_customers",
            "description": "List customers with no orders in the last N days, sorted by lifetime value.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Inactivity threshold in days (default 90)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_line_profitability",
            "description": "Per-line output, rejections, maintenance cost and downtime — shows which production line is losing money.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Analysis window in days (default 30)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_digital_twin",
            "description": "One snapshot of the whole factory: sales, profit, production, inventory, machine health, procurement, attendance.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
