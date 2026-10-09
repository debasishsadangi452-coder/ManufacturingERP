"""Product Costing Engine (Track B, TB-09 / TB-19).

Manufacturing cost never uses the customer selling price:

    Manufacturing cost = Raw material + Operation cost + Resource cost + Overhead

  Raw material    planned: BOM requirement x material unit cost
                  actual:  material consumed (ledger) x material unit cost
  Operation cost  routing cost_per_unit x units (planned / processed)
  Resource cost   machine hours x machine rate + labour hours x labour rate
                  (planned: setup + run minutes from routing; actual: worked minutes)
  Overhead        ManufacturingSettings.overhead_method:
                    per_unit             rate x units
                    percent_of_material  rate % x raw material cost
                    per_machine_hour     rate x machine hours
                    per_labour_hour      rate x labour hours
                  When no resource data and method is "none", the Accounting
                  Settings labour/overhead rates per unit are used, so the costing
                  screen and the GL posting agree.

Unit cost = total cost / good units. Variance = actual - standard cost of the
units actually produced (planned unit cost x output).
"""
from decimal import Decimal, ROUND_HALF_UP

from .models import ManufacturingSettings

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _component_item(req):
    """The component Item of a material requirement row.

    Recipe.material_requirements() yields BOMLine objects (component is
    ``raw_material``) when the product has a BOM, and RecipeIngredient objects
    (component is ``item``) otherwise. Resolve either shape to its Item.
    """
    return getattr(req, "raw_material", None) or req.item


def _component_item_id(req):
    rid = getattr(req, "raw_material_id", None)
    return rid if rid is not None else req.item_id


def _d(value):
    return Decimal(str(value or 0))


def _money(value):
    return _d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _material_unit_cost(item, company):
    from production.execution import unit_cost_of
    return unit_cost_of(item, company)


def _policy(company):
    try:
        from accounting.manufacturing_accounting import get_manufacturing_policy
        return get_manufacturing_policy(company)
    except Exception:
        return {"labor_enabled": False, "overhead_enabled": False,
                "labor_rate_per_unit": ZERO, "overhead_rate_per_unit": ZERO}


def _overhead(settings, units, material, machine_hours, labour_hours):
    rate = _d(settings.overhead_rate)
    method = settings.overhead_method
    if method == "per_unit":
        return rate * _d(units)
    if method == "percent_of_material":
        return material * rate / Decimal("100")
    if method == "per_machine_hour":
        return rate * _d(machine_hours)
    if method == "per_labour_hour":
        return rate * _d(labour_hours)
    return ZERO


def _side(order, settings, company, *, actual):
    """Cost breakdown for one side (planned or actual)."""
    materials = []
    material_total = ZERO
    if not order.is_rework:
        ledgers = {l.item_id: l for l in order.material_requirements.all()}
        for ing, required in order.recipe.material_requirements(order.quantity):
            # `ing` is a BOMLine (component = raw_material) or a RecipeIngredient
            # (component = item); resolve both to the same Item / id.
            comp_item = _component_item(ing)
            comp_item_id = _component_item_id(ing)
            qty = ledgers[comp_item_id].consumed_quantity if actual and comp_item_id in ledgers else (
                0 if actual else required
            )
            unit = _material_unit_cost(comp_item, company)
            amount = _d(qty) * unit
            material_total += amount
            materials.append({
                "item_id": comp_item_id, "item": comp_item.name, "quantity": round(float(qty), 6),
                "unit_cost": float(unit), "amount": float(_money(amount)),
            })
    if actual:
        for scrap in order.scrap_records.exclude(item=order.recipe.product):
            material_total += _d(scrap.cost_impact)

    operations = []
    op_total = machine_cost = labour_cost = ZERO
    machine_hours = labour_hours = 0.0
    for op in order.operations.select_related("machine", "manpower"):
        units = (op.completed_quantity + op.rejected_quantity + op.scrap_quantity + op.rework_quantity) if actual \
            else op.planned_quantity
        minutes = op.worked_minutes() if actual else op.planned_minutes
        hours = minutes / 60
        op_cost = _d(op.cost_per_unit) * _d(units)
        m_cost = _d(hours) * _d(op.machine.cost_per_hour) if op.machine_id else ZERO
        l_cost = _d(hours) * _d(op.manpower.cost_per_hour) if op.manpower_id else ZERO
        if op.machine_id:
            machine_hours += hours
        if op.manpower_id:
            labour_hours += hours
        op_total += op_cost
        machine_cost += m_cost
        labour_cost += l_cost
        operations.append({
            "operation_id": op.id, "sequence": op.sequence, "name": op.name,
            "units": round(units, 6), "hours": round(hours, 4),
            "operation_cost": float(_money(op_cost)),
            "machine_cost": float(_money(m_cost)), "labour_cost": float(_money(l_cost)),
        })

    units = order.produced_quantity if actual else order.quantity
    overhead = _overhead(settings, units, material_total, machine_hours, labour_hours)
    basis = "resources"
    if machine_cost + labour_cost + op_total == ZERO and settings.overhead_method == "none":
        policy = _policy(company)
        basis = "accounting_policy"
        if policy.get("labor_enabled"):
            labour_cost = _d(policy["labor_rate_per_unit"]) * _d(units)
        if policy.get("overhead_enabled"):
            overhead = _d(policy["overhead_rate_per_unit"]) * _d(units)

    resource_cost = machine_cost + labour_cost
    total = material_total + op_total + resource_cost + overhead
    return {
        "basis": basis,
        "units": round(float(units), 6),
        "raw_material_cost": float(_money(material_total)),
        "operation_cost": float(_money(op_total)),
        "machine_cost": float(_money(machine_cost)),
        "labour_cost": float(_money(labour_cost)),
        "resource_cost": float(_money(resource_cost)),
        "overhead_cost": float(_money(overhead)),
        "total_cost": float(_money(total)),
        "unit_cost": float((total / _d(units)).quantize(Decimal("0.0001"))) if units else 0.0,
        "machine_hours": round(machine_hours, 4),
        "labour_hours": round(labour_hours, 4),
        "materials": materials,
        "operations": operations,
        "_total": total,
    }


def order_cost(order):
    """Planned and actual cost of a production order, with variance."""
    company = order.recipe.product.company
    settings = ManufacturingSettings.for_company(company)
    planned = _side(order, settings, company, actual=False)
    actual = _side(order, settings, company, actual=True)
    from django.db.models import Sum
    accepted = order.qualitycheck_set.aggregate(a=Sum("accepted_quantity"))["a"]
    good_units = accepted if accepted is not None else order.produced_quantity
    planned_unit = (planned["_total"] / _d(order.quantity)) if order.quantity else ZERO
    standard_of_output = planned_unit * _d(order.produced_quantity)
    actual_total = actual["_total"]
    variance = actual_total - standard_of_output
    for side in (planned, actual):
        side.pop("_total")
    return {
        "production_order": order.id,
        "order_number": order.order_number,
        "product": order.recipe.product.name,
        "selling_price_excluded": True,
        "overhead_method": settings.overhead_method,
        "overhead_rate": float(settings.overhead_rate),
        "planned": planned,
        "actual": actual,
        "good_units": round(float(good_units or 0), 6),
        "actual_unit_cost_per_good_unit": (
            float((actual_total / _d(good_units)).quantize(Decimal("0.0001"))) if good_units else 0.0
        ),
        "standard_cost_of_output": float(_money(standard_of_output)),
        "variance": float(_money(variance)),
        "variance_percent": (
            float((variance / standard_of_output * 100).quantize(Decimal("0.01"))) if standard_of_output else 0.0
        ),
    }


def order_unit_cost(order):
    """Planned manufacturing cost per unit (used to value scrap)."""
    company = order.recipe.product.company
    side = _side(order, ManufacturingSettings.for_company(company), company, actual=False)
    return _d(side["unit_cost"])


def gl_cost_overrides(order):
    """(labour, overhead) amounts for the manufacturing journal entry when the
    order is costed from resources; (None, None) keeps the policy rates."""
    company = order.recipe.product.company
    settings = ManufacturingSettings.for_company(company)
    side = _side(order, settings, company, actual=True)
    if side["basis"] != "resources":
        return None, None
    labour = _money(side["labour_cost"])
    overhead = _money(side["machine_cost"]) + _money(side["operation_cost"]) + _money(side["overhead_cost"])
    if labour == ZERO and overhead == ZERO:
        return None, None
    return labour, overhead
