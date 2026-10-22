"""Profit of a sales order: what it earns minus what its goods cost to make.

Revenue is the order's price after discount (tax is not income). Each line is
costed per unit:
  * made for this order -> the production orders' actual cost per good unit
    (raw material + operations + machine + labour + utilities/overhead + added
    costs); until a run has output, its planned unit cost is used and flagged;
  * otherwise (sent from stock) -> the item's standard cost.
Cost is taken on the quantity ordered, or on the quantity delivered once the
order has been closed short.
"""
from decimal import Decimal

from accounting.auto_posting import standard_unit_cost
from production.costing import order_cost

D = Decimal


def _money(v):
    return float(D(str(v or 0)).quantize(D("0.01")))


def _unit_cost_from_production(order, item_id):
    runs = [p for p in order.production_orders.select_related("recipe__product") if p.recipe.product_id == item_id
            and p.status != "cancelled"]
    if not runs:
        return None, False
    actual = planned = qty_actual = qty_planned = 0.0
    for run in runs:
        cost = order_cost(run)
        planned += cost["planned"]["total_cost"]
        qty_planned += run.quantity
        if run.produced_quantity:
            actual += cost["actual"]["total_cost"]
            qty_actual += run.produced_quantity
    if qty_actual:
        return actual / qty_actual, False
    return (planned / qty_planned if qty_planned else 0.0), True


def sales_order_profit(order):
    closed_short = order.status == "delivered" and any(
        oi.shipped_quantity + 1e-6 < oi.quantity for oi in order.salesorderitem_set.all())
    lines, cost_total = [], 0.0
    estimated = False
    for oi in order.salesorderitem_set.select_related("item"):
        qty = oi.shipped_quantity if closed_short else oi.quantity
        unit_cost, is_estimate = _unit_cost_from_production(order, oi.item_id)
        source = "production"
        if unit_cost is None:
            unit_cost, source, is_estimate = float(standard_unit_cost(oi.item)[0] or 0), "standard cost", False
        estimated = estimated or is_estimate
        line_cost = unit_cost * qty
        cost_total += line_cost
        lines.append({
            "item": oi.item.name, "quantity": qty, "unit": oi.item.unit,
            "unit_price": float(oi.unit_price), "revenue": _money(D(str(oi.unit_price)) * D(str(qty))),
            "unit_cost": round(unit_cost, 4), "cost": _money(line_cost),
            "cost_source": source + (" (planned — not produced yet)" if is_estimate else ""),
        })
    gross = D(str(order.subtotal or 0)) if not closed_short else sum((D(str(l["revenue"])) for l in lines), D("0"))
    discount = D(str(order.discount_amount or 0))
    if closed_short and order.subtotal:
        discount = discount * gross / D(str(order.subtotal))
    revenue = gross - discount
    profit = revenue - D(str(cost_total))
    return {
        "id": order.id, "order": f"SO-{order.id}", "customer": order.customer.name, "status": order.status,
        "created_at": order.created_at, "closed_short": closed_short, "estimated": estimated,
        "revenue": _money(revenue), "cost": _money(cost_total), "profit": _money(profit),
        "margin_percent": round(float(profit / revenue * 100), 1) if revenue else 0.0,
        "lines": lines,
    }
