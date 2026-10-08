"""
Material Requirements Planning (MRP) Engine for Manufacturing ERP.

Implements Phase 2:
Production Plan -> BOM -> Multi-Level Explosion -> Semi-Finished & Raw Material Requirements ->
Inventory Availability Check -> Net Requirement & Shortage Identification.

Pure calculation service:
- Zero stock mutations
- Zero purchase orders created
"""

from decimal import Decimal
from typing import Dict, Any, List, Optional
from django.core.exceptions import ValidationError
from django.db.models import Sum

from inventory.models import Item, Stock, Warehouse, BOM, BOMLine
from inventory.uom import convert, UomConversionError


def detect_bom_cycle(parent_item: Item, component_item: Item, visited: Optional[set] = None) -> bool:
    """
    Check if adding component_item to parent_item's BOM would introduce a circular dependency.
    Returns True if a cycle is detected, False otherwise.
    """
    if not parent_item or not component_item:
        return False
    if parent_item.id == component_item.id:
        return True
    if visited is None:
        visited = set()
    if component_item.id in visited:
        return False
    visited.add(component_item.id)

    bom = getattr(component_item, "bom", None)
    if not bom:
        bom = BOM.objects.filter(finished_good=component_item).first()

    if bom:
        for line in bom.lines.select_related("raw_material").all():
            if line.raw_material_id == parent_item.id:
                return True
            if detect_bom_cycle(parent_item, line.raw_material, visited.copy()):
                return True
    return False


def get_active_bom(item: Item) -> BOM:
    """
    Retrieve the active BOM for an item.
    Enforces deterministic BOM resolution:
    - If multiple active BOMs exist: raises ValidationError
    - If no active BOM exists: raises ValidationError
    """
    active_boms = BOM.objects.filter(finished_good=item, is_active=True)
    count = active_boms.count()
    if count == 0:
        raise ValidationError(f"Active BOM not found for item '{item.name}'.")
    if count > 1:
        raise ValidationError(f"Multiple active BOMs found for item '{item.name}'. Please designate a single active BOM.")
    return active_boms.first()


def get_stock_available(item: Item, warehouse: Optional[Warehouse] = None, company=None) -> float:
    """
    Calculate on-hand available usable stock for an item using the existing Stock model.
    Respects warehouse scoping and company scoping.
    Strictly excludes quarantine warehouses from usable available stock.
    """
    qs = Stock.objects.filter(item=item, warehouse__is_quarantine=False)
    if warehouse:
        qs = qs.filter(warehouse=warehouse)
    elif company:
        qs = qs.filter(warehouse__company=company)

    total = qs.aggregate(t=Sum("quantity"))["t"]
    on_hand = float(total or 0.0)
    if item.category == "finished_good":
        from fulfillment.models import FGAllocation
        allocations = FGAllocation.objects.filter(item=item, status="active")
        if company:
            allocations = allocations.filter(company=company)
        allocated = allocations.aggregate(total=Sum("quantity"), dispatched=Sum("dispatched_quantity"))
        reserved = max(
            float(allocated["total"] or 0) - float(allocated["dispatched"] or 0), 0
        )
        on_hand = max(on_hand - reserved, 0)
    return on_hand


def _open_purchase_quantity(item: Item, company=None) -> float:
    from procurement.models import PurchaseOrderItem
    from django.db.models import Q

    rows = PurchaseOrderItem.objects.filter(
        item=item, purchase_order__status__in=("pending", "approved", "ordered")
    )
    if company:
        rows = rows.filter(
            Q(purchase_order__vendor__company=company)
            | Q(purchase_order__requisition__company=company)
        )
    total = 0.0
    for line in rows.select_related("unit_of_measure", "item__base_unit", "item__purchase_unit"):
        source_unit = line.unit_of_measure or item.purchase_unit
        if source_unit and item.base_unit and source_unit.id != item.base_unit_id:
            try:
                total += float(convert(line.quantity, source_unit, item.base_unit, item=item))
            except UomConversionError as exc:
                raise ValidationError(str(exc))
        else:
            total += float(line.quantity)
    return total


def _minimum_order_quantity_in_base(item: Item) -> float:
    minimum = float(item.minimum_order_quantity or 0)
    source_unit, target_unit = item.purchase_unit, item.base_unit
    if minimum > 0 and source_unit and target_unit and source_unit.id != target_unit.id:
        try:
            return float(convert(minimum, source_unit, target_unit, item=item))
        except UomConversionError as exc:
            raise ValidationError(str(exc))
    return minimum


def _convert_quantity_if_possible(qty: float, line_uom, item_base_uom, item=None) -> tuple[float, str]:
    """
    Converts line quantity to component item's base unit if possible.
    Returns (converted_quantity, uom_code_or_str).
    """
    if line_uom and item_base_uom and line_uom.id != item_base_uom.id:
        try:
            converted = float(convert(qty, line_uom, item_base_uom, item=item))
            return converted, item_base_uom.code
        except UomConversionError as e:
            raise ValidationError(str(e))
    elif line_uom:
        return float(qty), line_uom.code
    elif item_base_uom:
        return float(qty), item_base_uom.code
    return float(qty), "unit"


def explode_bom_tree(
    item: Item,
    quantity: float,
    level: int = 1,
    parent_item: Optional[Item] = None,
    visited_ids: Optional[set] = None,
    warehouse: Optional[Warehouse] = None,
    company=None
) -> Dict[str, Any]:
    """
    Recursively explode a BOM down to raw materials, building a complete hierarchical tree.
    """
    if visited_ids is None:
        visited_ids = set()

    if item.id in visited_ids:
        raise ValidationError(f"Circular BOM dependency detected involving item '{item.name}'.")

    current_visited = visited_ids.copy()
    current_visited.add(item.id)

    bom = BOM.objects.filter(finished_good=item, is_active=True).first()
    formula = None
    if bom:
        bom = get_active_bom(item)
    elif BOM.objects.filter(finished_good=item).exists():
        get_active_bom(item)
    else:
        from .models import Recipe

        formula = Recipe.objects.filter(product=item).first()
        if formula is None:
            get_active_bom(item)

    available_stock = get_stock_available(item, warehouse=warehouse, company=company)

    node = {
        "item_id": item.id,
        "item_name": item.name,
        "category": item.category,
        "sku": item.sku,
        "level": level,
        "bom_id": bom.id if bom else None,
        "bom_version": bom.version if bom else None,
        "formula_id": formula.id if formula else None,
        "formula_version": str(formula.id) if formula else None,
        "source": "bom" if bom else "formula",
        "required_quantity": quantity,
        "unit": item.base_unit.code if item.base_unit else item.unit,
        "available_stock": available_stock,
        "components": []
    }

    if bom:
        components = []
        for line in bom.lines.select_related("raw_material", "unit_of_measure", "raw_material__base_unit").all():
            comp = line.raw_material
            converted_line_qty, uom_str = _convert_quantity_if_possible(
                line.quantity, line.unit_of_measure, comp.base_unit, item=comp
            )
            if not uom_str or uom_str == "unit":
                uom_str = line.unit or comp.unit or "unit"
            components.append((line, comp, converted_line_qty, uom_str, quantity * converted_line_qty))
    else:
        components = []
        for ingredient, required_quantity in formula.material_requirements(quantity):
            comp = ingredient.item
            required_quantity = float(required_quantity)
            components.append((
                ingredient,
                comp,
                required_quantity / quantity,
                comp.base_unit.code if comp.base_unit else comp.unit or "unit",
                required_quantity,
            ))

    for line, comp, converted_line_qty, uom_str, component_required in components:
        comp_available = get_stock_available(comp, warehouse=warehouse, company=company)
        shortage = max(0.0, component_required - comp_available)

        component_data = {
            "bom_line_id": line.id if bom else None,
            "formula_line_id": line.id if formula else None,
            "item_id": comp.id,
            "item_name": comp.name,
            "category": comp.category,
            "level": level,
            "parent_item_id": item.id,
            "parent_item_name": item.name,
            "quantity_per_parent": converted_line_qty,
            "required_quantity": component_required,
            "unit": uom_str,
            "available_quantity": comp_available,
            "net_requirement": shortage,
            "shortage_quantity": shortage,
            "status": "shortage" if shortage > 0 else "available",
            "sub_components": []
        }

        # If component is semi-finished, explode recursively
        if comp.is_semi_finished and shortage > 0:
            try:
                sub_tree = explode_bom_tree(
                    item=comp,
                    quantity=shortage,
                    level=level + 1,
                    parent_item=item,
                    visited_ids=current_visited,
                    warehouse=warehouse,
                    company=company
                )
                component_data["sub_components"] = sub_tree["components"]
                component_data["child_bom_id"] = sub_tree["bom_id"]
                component_data["child_bom_version"] = sub_tree["bom_version"]
            except ValidationError as e:
                # If semi-finished has no active BOM, surface clear message or propagate
                if "Active BOM not found" in str(e):
                    raise ValidationError(f"Semi-finished component '{comp.name}' requires an active BOM: {str(e)}")
                raise

        node["components"].append(component_data)

    return node


def _collect_all_requirements(tree_components: List[Dict[str, Any]], flat_list: List[Dict[str, Any]]):
    """
    Recursively traverse tree components to gather every individual line requirement.
    """
    for comp in tree_components:
        flat_list.append({
            "bom_line_id": comp.get("bom_line_id"),
            "item_id": comp["item_id"],
            "item_name": comp["item_name"],
            "category": comp["category"],
            "level": comp["level"],
            "parent_item_id": comp.get("parent_item_id"),
            "parent_item_name": comp.get("parent_item_name"),
            "quantity_per_parent": comp["quantity_per_parent"],
            "required_quantity": comp["required_quantity"],
            "unit": comp["unit"],
        })
        if comp.get("sub_components"):
            _collect_all_requirements(comp["sub_components"], flat_list)


def calculate_mrp_for_plan(plan, warehouse: Optional[Warehouse] = None) -> Dict[str, Any]:
    """
    Main MRP Calculation Engine entry point.
    Takes a ProductionPlan instance (and optional warehouse) and returns
    comprehensive material requirements with inventory availability and shortages.
    """
    if not plan:
        raise ValidationError("Production plan is required.")

    if not plan.item:
        raise ValidationError("Production plan item is required.")

    if plan.planned_quantity <= 0:
        raise ValidationError("Production plan planned quantity must be greater than zero.")

    company = plan.company or (plan.customer.company if plan.customer else None)

    # 1. Explode BOM hierarchy recursively
    tree = explode_bom_tree(
        item=plan.item,
        quantity=float(plan.planned_quantity),
        level=1,
        warehouse=warehouse,
        company=company
    )

    # 2. Collect all individual component requirements
    line_details = []
    _collect_all_requirements(tree["components"], line_details)

    # 3. Consolidate gross requirements by item_id
    consolidated_map: Dict[int, Dict[str, Any]] = {}
    for line in line_details:
        item_id = line["item_id"]
        if item_id not in consolidated_map:
            item_obj = Item.objects.select_related("base_unit").get(id=item_id)
            available = get_stock_available(item_obj, warehouse=warehouse, company=company)
            consolidated_map[item_id] = {
                "item_id": item_obj.id,
                "item_name": item_obj.name,
                "category": item_obj.category,
                "sku": item_obj.sku,
                "unit": line["unit"],
                "required_quantity": 0.0,
                "available_quantity": available,
                "on_order_quantity": _open_purchase_quantity(item_obj, company=company),
                "safety_stock": float(item_obj.safety_stock or 0),
                "minimum_order_quantity": _minimum_order_quantity_in_base(item_obj),
                "lead_time_days": item_obj.lead_time_days,
                "parents": set(),
                "levels": set(),
            }
        consolidated_map[item_id]["required_quantity"] += line["required_quantity"]
        if line.get("parent_item_name"):
            consolidated_map[item_id]["parents"].add(line["parent_item_name"])
        consolidated_map[item_id]["levels"].add(line["level"])

    # 4. Finalize net requirement, shortage, and status
    consolidated_list = []
    semi_finished_list = []
    raw_materials_list = []

    for item_id, data in consolidated_map.items():
        req_qty = round(data["required_quantity"], 4)
        avail_qty = round(data["available_quantity"], 4)
        from .models import ManufacturingSettings
        settings = ManufacturingSettings.for_company(company)
        on_order_qty = round(data["on_order_quantity"], 4) if settings.include_open_purchase_orders_in_mrp else 0.0
        safety_stock = round(data["safety_stock"], 4) if settings.protect_safety_stock_in_mrp else 0.0
        net_req = round(max(0.0, req_qty + safety_stock - avail_qty - on_order_qty), 4)
        shortage = net_req
        recommended_purchase_quantity = (
            max(shortage, data["minimum_order_quantity"]) if shortage > 0 else 0.0
        )
        purchase_by_date = None
        if plan.target_date and data["lead_time_days"]:
            from datetime import timedelta
            purchase_by_date = (
                plan.target_date - timedelta(days=data["lead_time_days"])
            ).isoformat()
        if shortage > 0:
            status = "shortage"
        elif req_qty > 0 and avail_qty < req_qty + safety_stock:
            status = "incoming"
        else:
            status = "available"

        item_result = {
            "item_id": data["item_id"],
            "item_name": data["item_name"],
            "category": data["category"],
            "sku": data["sku"],
            "required_quantity": req_qty,
            "unit": data["unit"],
            "available_quantity": avail_qty,
            "on_order_quantity": on_order_qty,
            "safety_stock": safety_stock,
            "net_requirement": net_req,
            "shortage_quantity": shortage,
            "recommended_purchase_quantity": round(recommended_purchase_quantity, 4),
            "lead_time_days": data["lead_time_days"],
            "purchase_by_date": purchase_by_date,
            "status": status,
            "used_by": sorted(list(data["parents"])),
            "levels": sorted(list(data["levels"])),
            "min_level": min(data["levels"]) if data["levels"] else 1
        }
        consolidated_list.append(item_result)

        if data["category"] in ("intermediate", "semi_finished"):
            semi_finished_list.append(item_result)
        else:
            raw_materials_list.append(item_result)

    # Sort items logically by BOM level and item name
    consolidated_list.sort(key=lambda x: (x["min_level"], x["category"] not in ("intermediate", "semi_finished"), x["item_name"]))
    semi_finished_list.sort(key=lambda x: (x["min_level"], x["item_name"]))
    raw_materials_list.sort(key=lambda x: (x["min_level"], x["item_name"]))

    has_shortage = any(c["shortage_quantity"] > 0 for c in consolidated_list)

    return {
        "production_plan_id": plan.id,
        "plan_number": plan.plan_number,
        "product": {
            "id": plan.item.id,
            "name": plan.item.name,
            "category": plan.item.category,
            "sku": plan.item.sku,
            "unit": plan.item.unit
        },
        "planned_quantity": float(plan.planned_quantity),
        "bom": (
            {"id": tree["bom_id"], "version": tree["bom_version"]}
            if tree["source"] == "bom" else None
        ),
        "formula": (
            {"id": tree["formula_id"], "version": tree["formula_version"]}
            if tree["source"] == "formula" else None
        ),
        "source": tree["source"],
        "warehouse_id": warehouse.id if warehouse else None,
        "warehouse_name": warehouse.name if warehouse else ("All Warehouses" if not company else "Company Warehouses"),
        "has_shortage": has_shortage,
        "tree": tree,
        "semi_finished": semi_finished_list,
        "raw_materials": raw_materials_list,
        "consolidated": consolidated_list,
        "line_details": line_details
    }
