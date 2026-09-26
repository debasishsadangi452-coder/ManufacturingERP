"""
Manufacturing-to-Accounting Integration Subledger (Blueprint Section No. 15)
Integrates production orders, BOM material consumption, direct labor, applied overhead,
WIP accumulation, finished goods completion, scrap loss, and cost variances
directly with the Double-Entry Engine (#8), General Ledger (#9), and Inventory Valuation (#14).
Enforces policy configuration, period lock boundaries, idempotent GL posting, and complete audit traceability.
"""

import math
from decimal import Decimal
from datetime import date, datetime
from django.db import transaction
from django.db.models import Sum, Q, F
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import Company
from production.models import ProductionOrder, Recipe, RecipeIngredient, ProductionLine
from inventory.models import Item, Warehouse, Stock, StockMovement
from procurement.models import PurchaseOrderItem
from accounting.models import Account, AccountType, JournalEntry, JournalEntryLine, AccountingSettings, AccountingPeriod
from accounting.engine import post_journal_entry, reverse_journal_entry
from accounting.inventory_accounting import resolve_inventory_asset_account


def get_manufacturing_policy(company):
    """
    Returns the configured manufacturing accounting policy for the tenant.
    Reads from AccountingSettings with intelligent company-safe defaults.
    """
    settings = AccountingSettings.objects.filter(company=company).first()
    if not settings:
        return {
            "enabled": True,
            "wip_enabled": True,
            "labor_enabled": False,
            "overhead_enabled": False,
            "scrap_enabled": True,
            "variance_enabled": True,
            "labor_rate_per_unit": Decimal("0.00"),
            "overhead_rate_per_unit": Decimal("0.00"),
            "wip_account_id": None,
            "raw_material_account_id": None,
            "finished_goods_account_id": None,
            "labor_account_id": None,
            "overhead_account_id": None,
            "scrap_account_id": None,
            "variance_account_id": None,
        }
    return {
        "enabled": bool(settings.manufacturing_accounting_enabled),
        "wip_enabled": bool(settings.wip_accounting_enabled),
        "labor_enabled": bool(settings.labor_accounting_enabled),
        "overhead_enabled": bool(settings.overhead_accounting_enabled),
        "scrap_enabled": bool(settings.scrap_accounting_enabled),
        "variance_enabled": bool(settings.variance_accounting_enabled),
        "labor_rate_per_unit": Decimal(str(settings.labor_rate_per_unit or "0.00")),
        "overhead_rate_per_unit": Decimal(str(settings.overhead_rate_per_unit or "0.00")),
        "wip_account_id": settings.manufacturing_wip_account_id,
        "raw_material_account_id": settings.inventory_raw_material_account_id,
        "finished_goods_account_id": settings.inventory_finished_goods_account_id,
        "labor_account_id": settings.manufacturing_labor_account_id,
        "overhead_account_id": settings.manufacturing_overhead_account_id,
        "scrap_account_id": settings.manufacturing_scrap_account_id,
        "variance_account_id": settings.manufacturing_variance_account_id,
    }


def resolve_manufacturing_wip_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Work-in-Progress (WIP) asset account.
    Standard code: 1220 Work-in-Progress (WIP) under 1200 Inventories / 1000 Current Assets.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified WIP account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post WIP to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.manufacturing_wip_account:
        return settings.manufacturing_wip_account

    # 1. Code 1220 Work-in-Progress (WIP)
    acc = Account.objects.filter(company=company, code="1220", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 2. Check by name
    acc = Account.objects.filter(
        company=company,
        name__icontains="Work-in-Progress",
        account_type__category="asset",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    acc = Account.objects.filter(
        company=company,
        name__icontains="WIP",
        account_type__category="asset",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 3. Provision standard 1220 Work-in-Progress (WIP)
    parent = Account.objects.filter(company=company, code="1200").first() or Account.objects.filter(company=company, code="1000").first()
    acc_type = AccountType.objects.filter(name="Inventory").first() or AccountType.objects.filter(category="asset").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="1220",
        defaults={
            "name": "Work-in-Progress (WIP)",
            "account_type": acc_type,
            "parent": parent,
            "description": "Items and materials currently in active production batches",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_manufacturing_raw_material_account(item, company, account_id=None):
    """
    Resolves the active leaf Raw Material Inventory asset account.
    Standard code: 1210 Raw Materials Inventory.
    """
    return resolve_inventory_asset_account(item, company, account_id=account_id)


def resolve_manufacturing_finished_goods_account(item, company, account_id=None):
    """
    Resolves the active leaf Finished Goods Inventory asset account.
    Standard code: 1230 Finished Goods Inventory.
    """
    return resolve_inventory_asset_account(item, company, account_id=account_id)


def resolve_manufacturing_labor_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Direct Labor Clearing / Accrued Wages account.
    Standard code: 2100 Accrued Payroll & Wages or 5100 Direct Manufacturing Labor.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified labor clearing account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post labor to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.manufacturing_labor_account:
        return settings.manufacturing_labor_account

    # 1. Code 2100 Accrued Payroll & Wages
    acc = Account.objects.filter(company=company, code="2100", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 2. Code 5100 Direct Manufacturing Labor
    acc = Account.objects.filter(company=company, code="5100", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 3. Name lookup
    acc = Account.objects.filter(
        company=company,
        name__icontains="Payroll",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 4. Provision standard 2100 Accrued Payroll & Wages
    parent = Account.objects.filter(company=company, code="2000").first()
    acc_type = AccountType.objects.filter(name="Accrued Expenses & Other Current Liabilities").first() or AccountType.objects.filter(category="liability").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="2100",
        defaults={
            "name": "Accrued Payroll & Direct Labor Clearing",
            "account_type": acc_type,
            "parent": parent,
            "description": "Accrued direct labor compensation and payroll clearing for production",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_manufacturing_overhead_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Manufacturing Overhead Applied account.
    Standard code: 5040 Manufacturing Overhead Applied or 5200 Factory Power & Utilities.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified overhead account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post overhead to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.manufacturing_overhead_account:
        return settings.manufacturing_overhead_account

    # 1. Code 5040 Manufacturing Overhead Applied
    acc = Account.objects.filter(company=company, code="5040", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 2. Code 5200 Factory Power & Utilities
    acc = Account.objects.filter(company=company, code="5200", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 3. Name lookup
    acc = Account.objects.filter(
        company=company,
        name__icontains="Overhead",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 4. Provision 5040 Manufacturing Overhead Applied under 5000 COGS
    parent = Account.objects.filter(company=company, code="5000").first()
    acc_type = AccountType.objects.filter(name="Cost of Goods Sold (Manufacturing Overhead)").first() or AccountType.objects.filter(category="expense").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="5040",
        defaults={
            "name": "Manufacturing Overhead Applied",
            "account_type": acc_type,
            "parent": parent,
            "description": "Standard absorbed factory overhead applied to active production batches",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_manufacturing_scrap_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Production Scrap / Loss Expense account.
    Standard code: 5080 Production Scrap & Waste Loss or 6520 Inventory Loss & Write-off.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified scrap account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post scrap to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.manufacturing_scrap_account:
        return settings.manufacturing_scrap_account

    # 1. Code 5080 Production Scrap
    acc = Account.objects.filter(company=company, code="5080", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 2. Code 6520 Inventory Write-off
    acc = Account.objects.filter(company=company, code="6520", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 3. Name lookup
    acc = Account.objects.filter(
        company=company,
        name__icontains="Scrap",
        account_type__category="expense",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 4. Provision standard 5080 Production Scrap & Waste Loss under 5000 COGS
    parent = Account.objects.filter(company=company, code="5000").first()
    acc_type = AccountType.objects.filter(name="Cost of Goods Sold (Raw Materials)").first() or AccountType.objects.filter(category="expense").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="5080",
        defaults={
            "name": "Production Scrap & Defective Loss",
            "account_type": acc_type,
            "parent": parent,
            "description": "Cost of defective materials, spoiled batches, and production scrap written off",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_manufacturing_variance_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Manufacturing Variance account.
    Standard code: 5090 Manufacturing Cost Variance.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified variance account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post variance to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.manufacturing_variance_account:
        return settings.manufacturing_variance_account

    # 1. Code 5090
    acc = Account.objects.filter(company=company, code="5090", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 2. Name lookup
    acc = Account.objects.filter(
        company=company,
        name__icontains="Manufacturing Variance",
        account_type__category="expense",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 3. Provision standard 5090 Manufacturing Cost Variance
    parent = Account.objects.filter(company=company, code="5000").first()
    acc_type = AccountType.objects.filter(name="Cost of Goods Sold (Raw Materials)").first() or AccountType.objects.filter(category="expense").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="5090",
        defaults={
            "name": "Manufacturing Cost Variance",
            "account_type": acc_type,
            "parent": parent,
            "description": "Unabsorbed manufacturing cost variances, material price variances, and yield differences",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def calculate_item_unit_cost(item, company):
    """
    Calculates unit cost for an inventory item reusing Section #14 valuation logic.
    """
    item_cost = Decimal(str(getattr(item, "purchase_cost", "0.00") or "0.00"))
    if item_cost > Decimal("0.00"):
        return item_cost.quantize(Decimal("0.01"))

    poi = PurchaseOrderItem.objects.filter(
        item=item,
        po__vendor__company=company
    ).order_by("-id").first()
    if poi and poi.unit_price > Decimal("0.00"):
        return Decimal(str(poi.unit_price)).quantize(Decimal("0.01"))

    selling_price = Decimal(str(getattr(item, "selling_price", "0.00") or "0.00"))
    if selling_price > Decimal("0.00"):
        return selling_price.quantize(Decimal("0.01"))

    return Decimal("1.00")


def calculate_production_order_costs(order, company, policy=None, labor_cost_override=None, overhead_cost_override=None):
    """
    Calculates detailed cost accumulation for a production order:
    - Raw Material Costs (from recipe ingredients & #14 inventory valuation)
    - Direct Labor Cost (from configured labor rate or override)
    - Applied Overhead Cost (from configured overhead rate or override)
    - Total Accumulated Production Cost
    - Unit Production Cost
    - Completed Finished Goods Value
    - Work-in-Progress (WIP) Remaining Balance
    """
    if policy is None:
        policy = get_manufacturing_policy(company)

    recipe = order.recipe
    planned_qty = Decimal(str(order.quantity or 0.0))
    batch_size = Decimal(str(getattr(recipe, "batch_size", 1.0) or 1.0))
    if batch_size <= Decimal("0.00"):
        batch_size = Decimal("1.00")

    batches = Decimal(str(math.ceil(float(planned_qty) / float(batch_size)))) if planned_qty > 0 else Decimal("0.00")

    materials = []
    total_material_cost = Decimal("0.00")

    ingredients = recipe.recipeingredient_set.select_related("item").all()
    for ing in ingredients:
        required_qty = (Decimal(str(ing.quantity)) * batches).quantize(Decimal("0.0001"))
        unit_cost = calculate_item_unit_cost(ing.item, company)
        line_cost = (required_qty * unit_cost).quantize(Decimal("0.01"))
        total_material_cost += line_cost

        materials.append({
            "item_id": ing.item.id,
            "item_name": ing.item.name,
            "sku": getattr(ing.item, "sku", ""),
            "quantity_required": float(required_qty),
            "unit_cost": float(unit_cost),
            "total_cost": float(line_cost),
        })

    # Direct Labor
    if labor_cost_override is not None:
        labor_cost = Decimal(str(labor_cost_override)).quantize(Decimal("0.01"))
    elif policy["labor_enabled"]:
        labor_cost = (planned_qty * policy["labor_rate_per_unit"]).quantize(Decimal("0.01"))
    else:
        labor_cost = Decimal("0.00")

    # Applied Overhead
    if overhead_cost_override is not None:
        overhead_cost = Decimal(str(overhead_cost_override)).quantize(Decimal("0.01"))
    elif policy["overhead_enabled"]:
        overhead_cost = (planned_qty * policy["overhead_rate_per_unit"]).quantize(Decimal("0.01"))
    else:
        overhead_cost = Decimal("0.00")

    total_production_cost = (total_material_cost + labor_cost + overhead_cost).quantize(Decimal("0.01"))

    unit_production_cost = (
        (total_production_cost / planned_qty).quantize(Decimal("0.0001"))
        if planned_qty > Decimal("0.00")
        else Decimal("0.0000")
    )

    return {
        "planned_quantity": float(planned_qty),
        "batch_size": float(batch_size),
        "batches": int(batches),
        "materials": materials,
        "total_material_cost": total_material_cost,
        "labor_cost": labor_cost,
        "overhead_cost": overhead_cost,
        "total_production_cost": total_production_cost,
        "unit_production_cost": unit_production_cost,
    }


def determine_production_order_accounting_requirement(order, company):
    """
    Determines whether a production order requires manufacturing accounting.
    Returns:
      (requires_accounting: bool, reason: str, event_subtype: str)
    """
    policy = get_manufacturing_policy(company)
    if not policy["enabled"]:
        return False, "Manufacturing accounting is disabled in Accounting Settings.", "disabled"

    if order.quantity <= 0:
        return False, "Production order has zero or negative quantity.", "zero_quantity"

    if not order.recipe or not order.recipe.product:
        return False, "Production order has no valid recipe or finished good product.", "missing_recipe"

    if order.status not in ["running", "completed"]:
        return False, f"Production order status '{order.status}' is not eligible for accounting posting (must be running or completed).", "ineligible_status"

    return True, f"Production order #{order.id} ({order.recipe.product.name}) is eligible for manufacturing accounting.", "production_order"


def get_manufacturing_accounting_preview(
    order_id,
    company,
    completed_qty=None,
    scrap_qty=None,
    labor_cost_override=None,
    overhead_cost_override=None,
    wip_account_id=None,
    fg_account_id=None,
):
    """
    Computes and returns a prospective balanced double-entry preview for a production order
    without committing any changes to the database.
    Demonstrates the complete cost flow:
    1. Raw Material Inventory -> WIP (material consumption)
    2. Direct Labor Clearing -> WIP (applied labor)
    3. Overhead Clearing -> WIP (applied overhead)
    4. WIP -> Finished Goods Inventory (completed units)
    5. WIP -> Scrap Loss Expense (scrapped units if any)
    """
    try:
        order = ProductionOrder.objects.select_related("recipe__product", "warehouse", "line").get(
            pk=order_id,
            recipe__product__company=company,
        )
    except ProductionOrder.DoesNotExist:
        raise ValidationError(_(f"Production order #{order_id} not found for company '{company.name}'."))

    policy = get_manufacturing_policy(company)
    costs = calculate_production_order_costs(
        order,
        company,
        policy=policy,
        labor_cost_override=labor_cost_override,
        overhead_cost_override=overhead_cost_override,
    )

    planned_qty = Decimal(str(costs["planned_quantity"]))
    effective_completed_qty = (
        Decimal(str(completed_qty))
        if completed_qty is not None
        else (planned_qty if order.status == "completed" else Decimal("0.00"))
    )
    if effective_completed_qty > planned_qty:
        raise ValidationError(_(f"Completed quantity ({effective_completed_qty}) cannot exceed planned quantity ({planned_qty})."))

    effective_scrap_qty = Decimal(str(scrap_qty or 0.0))
    if effective_completed_qty + effective_scrap_qty > planned_qty:
        raise ValidationError(_(f"Sum of completed ({effective_completed_qty}) and scrap ({effective_scrap_qty}) exceeds planned quantity ({planned_qty})."))

    unit_cost = costs["unit_production_cost"]
    fg_value = (effective_completed_qty * unit_cost).quantize(Decimal("0.01"))
    scrap_value = (effective_scrap_qty * unit_cost).quantize(Decimal("0.01"))
    total_cost = costs["total_production_cost"]
    remaining_wip = (total_cost - fg_value - scrap_value).quantize(Decimal("0.01"))

    # Resolve Accounts
    wip_acc = resolve_manufacturing_wip_account(company, account_id=wip_account_id)
    fg_acc = resolve_manufacturing_finished_goods_account(order.recipe.product, company, account_id=fg_account_id)
    labor_acc = resolve_manufacturing_labor_account(company)
    overhead_acc = resolve_manufacturing_overhead_account(company)
    scrap_acc = resolve_manufacturing_scrap_account(company)

    lines_preview = []

    # 1. Raw Materials Consumed: DR WIP, CR Raw Material Asset
    for mat in costs["materials"]:
        line_amt = Decimal(str(mat["total_cost"]))
        if line_amt <= Decimal("0.00"):
            continue
        raw_item = Item.objects.get(pk=mat["item_id"])
        raw_acc = resolve_manufacturing_raw_material_account(raw_item, company)

        # DR WIP
        lines_preview.append({
            "account_id": wip_acc.id,
            "account_code": wip_acc.code,
            "account_name": wip_acc.name,
            "category": wip_acc.account_type.category,
            "type": "debit",
            "debit": float(line_amt),
            "credit": 0.0,
            "description": f"Material Consumption: {mat['item_name']} (Qty: {mat['quantity_required']}) → WIP",
        })
        # CR Raw Material
        lines_preview.append({
            "account_id": raw_acc.id,
            "account_code": raw_acc.code,
            "account_name": raw_acc.name,
            "category": raw_acc.account_type.category,
            "type": "credit",
            "debit": 0.0,
            "credit": float(line_amt),
            "description": f"Raw Material Issued: {mat['item_name']} (PO #{order.id})",
        })

    # 2. Direct Labor Applied (if > 0)
    if costs["labor_cost"] > Decimal("0.00"):
        l_amt = costs["labor_cost"]
        lines_preview.append({
            "account_id": wip_acc.id,
            "account_code": wip_acc.code,
            "account_name": wip_acc.name,
            "category": wip_acc.account_type.category,
            "type": "debit",
            "debit": float(l_amt),
            "credit": 0.0,
            "description": f"Direct Labor Applied to PO #{order.id} ({planned_qty} units @ {policy['labor_rate_per_unit']}/unit)",
        })
        lines_preview.append({
            "account_id": labor_acc.id,
            "account_code": labor_acc.code,
            "account_name": labor_acc.name,
            "category": labor_acc.account_type.category,
            "type": "credit",
            "debit": 0.0,
            "credit": float(l_amt),
            "description": f"Accrued Payroll & Direct Labor Clearing (PO #{order.id})",
        })

    # 3. Manufacturing Overhead Applied (if > 0)
    if costs["overhead_cost"] > Decimal("0.00"):
        o_amt = costs["overhead_cost"]
        lines_preview.append({
            "account_id": wip_acc.id,
            "account_code": wip_acc.code,
            "account_name": wip_acc.name,
            "category": wip_acc.account_type.category,
            "type": "debit",
            "debit": float(o_amt),
            "credit": 0.0,
            "description": f"Factory Overhead Applied to PO #{order.id} ({planned_qty} units @ {policy['overhead_rate_per_unit']}/unit)",
        })
        lines_preview.append({
            "account_id": overhead_acc.id,
            "account_code": overhead_acc.code,
            "account_name": overhead_acc.name,
            "category": overhead_acc.account_type.category,
            "type": "credit",
            "debit": 0.0,
            "credit": float(o_amt),
            "description": f"Manufacturing Overhead Applied Clearing (PO #{order.id})",
        })

    # 4. Production Completion: DR Finished Goods Inventory, CR WIP
    if fg_value > Decimal("0.00"):
        lines_preview.append({
            "account_id": fg_acc.id,
            "account_code": fg_acc.code,
            "account_name": fg_acc.name,
            "category": fg_acc.account_type.category,
            "type": "debit",
            "debit": float(fg_value),
            "credit": 0.0,
            "description": f"Production Completion: {order.recipe.product.name} ({effective_completed_qty} units @ ${unit_cost}/unit) → FG",
        })
        lines_preview.append({
            "account_id": wip_acc.id,
            "account_code": wip_acc.code,
            "account_name": wip_acc.name,
            "category": wip_acc.account_type.category,
            "type": "credit",
            "debit": 0.0,
            "credit": float(fg_value),
            "description": f"WIP Cost Transferred to Finished Goods (PO #{order.id})",
        })

    # 5. Production Scrap: DR Scrap Expense, CR WIP
    if scrap_value > Decimal("0.00"):
        lines_preview.append({
            "account_id": scrap_acc.id,
            "account_code": scrap_acc.code,
            "account_name": scrap_acc.name,
            "category": scrap_acc.account_type.category,
            "type": "debit",
            "debit": float(scrap_value),
            "credit": 0.0,
            "description": f"Production Scrap Loss: {effective_scrap_qty} units of {order.recipe.product.name} (PO #{order.id})",
        })
        lines_preview.append({
            "account_id": wip_acc.id,
            "account_code": wip_acc.code,
            "account_name": wip_acc.name,
            "category": wip_acc.account_type.category,
            "type": "credit",
            "debit": 0.0,
            "credit": float(scrap_value),
            "description": f"WIP Relieved for Scrapped Units (PO #{order.id})",
        })

    total_debit = sum(Decimal(str(l["debit"])) for l in lines_preview)
    total_credit = sum(Decimal(str(l["credit"])) for l in lines_preview)
    is_balanced = total_debit == total_credit

    # Check existing journal entry
    existing_je = JournalEntry.objects.filter(
        company=company,
        source_module="manufacturing",
        source_id=order.id,
        status="posted",
    ).first()

    return {
        "order_id": order.id,
        "product_name": order.recipe.product.name,
        "sku": getattr(order.recipe.product, "sku", ""),
        "warehouse_name": order.warehouse.name,
        "planned_quantity": float(planned_qty),
        "completed_quantity": float(effective_completed_qty),
        "scrap_quantity": float(effective_scrap_qty),
        "status": order.status,
        "is_partial": float(effective_completed_qty) < float(planned_qty),
        "costs": {
            "total_material_cost": float(costs["total_material_cost"]),
            "labor_cost": float(costs["labor_cost"]),
            "overhead_cost": float(costs["overhead_cost"]),
            "total_production_cost": float(total_cost),
            "unit_production_cost": float(unit_cost),
            "finished_goods_value": float(fg_value),
            "scrap_value": float(scrap_value),
            "remaining_wip_balance": float(remaining_wip),
        },
        "lines": lines_preview,
        "total_debit": float(total_debit),
        "total_credit": float(total_credit),
        "is_balanced": is_balanced,
        "existing_journal_entry_id": existing_je.id if existing_je else None,
        "existing_journal_entry_number": existing_je.entry_number if existing_je else None,
        "requires_accounting": is_balanced and len(lines_preview) > 0,
    }


@transaction.atomic
def post_manufacturing_accounting(
    order_id,
    company,
    user=None,
    notes=None,
    completed_qty=None,
    scrap_qty=None,
    labor_cost_override=None,
    overhead_cost_override=None,
    accounting_date=None,
):
    """
    Atomically generates and posts a balanced double-entry journal entry for a production order
    via the Double-Entry Engine (#8), establishing the complete cost flow:
    Raw Materials -> WIP -> Finished Goods / Scrap.
    Enforces idempotency, lock dates, and fiscal period validations.
    """
    try:
        order = ProductionOrder.objects.select_related("recipe__product", "warehouse").get(
            pk=order_id,
            recipe__product__company=company,
        )
    except ProductionOrder.DoesNotExist:
        raise ValidationError(_(f"Production order #{order_id} not found for company '{company.name}'."))

    # Idempotency check: prevent duplicate journal entry
    existing_je = JournalEntry.objects.filter(
        company=company,
        source_module="manufacturing",
        source_id=order.id,
        status="posted",
    ).first()
    if existing_je:
        return existing_je, False

    requires_accounting, reason, event_subtype = determine_production_order_accounting_requirement(order, company)
    if not requires_accounting:
        raise ValidationError(_(f"Manufacturing accounting not required for PO #{order.id}: {reason}"))

    preview = get_manufacturing_accounting_preview(
        order_id=order.id,
        company=company,
        completed_qty=completed_qty,
        scrap_qty=scrap_qty,
        labor_cost_override=labor_cost_override,
        overhead_cost_override=overhead_cost_override,
    )

    if not preview["is_balanced"]:
        raise ValidationError(_("Manufacturing journal entry lines do not balance. Posting aborted."))

    if len(preview["lines"]) == 0:
        raise ValidationError(_("No accounting lines generated for this production order."))

    txn_date = accounting_date or timezone.now().date()
    if isinstance(txn_date, str):
        txn_date = datetime.strptime(txn_date, "%Y-%m-%d").date()

    # Lock date and fiscal period validation
    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.lock_date and txn_date <= settings.lock_date:
        raise ValidationError(_(f"Cannot post transaction on {txn_date}: period is locked as of {settings.lock_date}."))

    period = AccountingPeriod.objects.filter(
        fiscal_year__company=company,
        start_date__lte=txn_date,
        end_date__gte=txn_date,
    ).first()
    if period and getattr(period, "status", "") == "closed":
        raise ValidationError(_(f"Accounting period '{period.name}' is closed. Cannot post manufacturing entry."))

    ref = f"MFG-PO-{order.id}"
    desc = notes or (
        f"Manufacturing Accounting: PO #{order.id} · {order.recipe.product.name} · "
        f"Planned {preview['planned_quantity']}, Completed {preview['completed_quantity']}, Scrap {preview['scrap_quantity']} "
        f"(Total: ${preview['costs']['total_production_cost']:.2f}, FG: ${preview['costs']['finished_goods_value']:.2f})"
    )

    # 1. Create draft journal entry header
    entry = JournalEntry.objects.create(
        company=company,
        transaction_date=txn_date,
        accounting_period=period,
        reference=ref,
        description=desc,
        source_module="manufacturing",
        source_id=order.id,
        created_by=user,
        status="draft",
    )

    # 2. Create lines
    for idx, l in enumerate(preview["lines"], start=1):
        account = Account.objects.get(pk=l["account_id"])
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=account,
            line_number=idx,
            debit=Decimal(str(l["debit"])),
            credit=Decimal(str(l["credit"])),
            description=l["description"],
        )

    # 3. Post via Double-Entry Engine (#8)
    posted_je = post_journal_entry(entry.id, user=user)
    return posted_je, True


@transaction.atomic
def reverse_manufacturing_accounting(order_id, company, user=None, reason=None):
    """
    Safely reverses an existing posted manufacturing journal entry via the #8 Engine,
    preserving full audit trail and preventing orphan balances.
    """
    try:
        order = ProductionOrder.objects.get(
            pk=order_id,
            recipe__product__company=company,
        )
    except ProductionOrder.DoesNotExist:
        raise ValidationError(_(f"Production order #{order_id} not found for company '{company.name}'."))

    posted_je = JournalEntry.objects.filter(
        company=company,
        source_module="manufacturing",
        source_id=order.id,
        status="posted",
    ).first()

    if not posted_je:
        raise ValidationError(_(f"No active posted manufacturing journal entry found for Production Order #{order.id}."))

    reversal_reason = reason or f"Reversal of manufacturing accounting for Production Order #{order.id}"
    reversal_je = reverse_journal_entry(entry_id=posted_je.id, user=user, reason=reversal_reason)

    posted_je.refresh_from_db()
    return reversal_je


@transaction.atomic
def post_manufacturing_variance_adjustment(order_id, company, user=None, variance_amount=None, reason=None):
    """
    Records an unabsorbed manufacturing variance or standard costing adjustment for a production order.
    - If positive (unfavorable variance / excess cost): DR Manufacturing Variance (5090), CR WIP (1220)
    - If negative (favorable variance / cost savings): DR WIP (1220), CR Manufacturing Variance (5090)
    """
    try:
        order = ProductionOrder.objects.select_related("recipe__product").get(
            pk=order_id,
            recipe__product__company=company,
        )
    except ProductionOrder.DoesNotExist:
        raise ValidationError(_(f"Production order #{order_id} not found."))

    v_amt = Decimal(str(variance_amount or 0.0)).quantize(Decimal("0.01"))
    if v_amt == Decimal("0.00"):
        raise ValidationError(_("Variance adjustment amount cannot be zero."))

    wip_acc = resolve_manufacturing_wip_account(company)
    var_acc = resolve_manufacturing_variance_account(company)

    txn_date = timezone.now().date()
    abs_amt = abs(v_amt)

    if v_amt > Decimal("0.00"):
        # Unfavorable variance: DR Variance Expense, CR WIP Asset
        lines = [
            {"account_id": var_acc.id, "debit": abs_amt, "credit": Decimal("0.00"), "description": f"Unfavorable Manufacturing Variance - PO #{order.id}"},
            {"account_id": wip_acc.id, "debit": Decimal("0.00"), "credit": abs_amt, "description": f"WIP Variance Relieved - PO #{order.id}"},
        ]
    else:
        # Favorable variance: DR WIP Asset, CR Variance Gain
        lines = [
            {"account_id": wip_acc.id, "debit": abs_amt, "credit": Decimal("0.00"), "description": f"Favorable Manufacturing Variance - PO #{order.id}"},
            {"account_id": var_acc.id, "debit": Decimal("0.00"), "credit": abs_amt, "description": f"WIP Variance Gain - PO #{order.id}"},
        ]

    ref = f"MFG-VAR-{order.id}"
    desc = reason or f"Manufacturing Cost Variance Adjustment for PO #{order.id} ({order.recipe.product.name})"

    period = AccountingPeriod.objects.filter(
        fiscal_year__company=company,
        start_date__lte=txn_date,
        end_date__gte=txn_date,
    ).first()

    entry = JournalEntry.objects.create(
        company=company,
        transaction_date=txn_date,
        accounting_period=period,
        reference=ref,
        description=desc,
        source_module="manufacturing",
        source_id=order.id,
        created_by=user,
        status="draft",
    )

    for idx, l in enumerate(lines, start=1):
        account = Account.objects.get(pk=l["account_id"])
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=account,
            line_number=idx,
            debit=Decimal(str(l["debit"])),
            credit=Decimal(str(l["credit"])),
            description=l["description"],
        )

    posted_je = post_journal_entry(entry.id, user=user)
    return posted_je


def get_manufacturing_accounting_summary(company):
    """
    Aggregates manufacturing subledger KPIs:
    - Current WIP Balance in GL (account 1220 net debit balance)
    - Total Finished Goods Output Value
    - Total Production Orders Count
    - Posted to Accounting Count
    - Pending Accounting Count
    - Scrap & Waste Loss Expense
    - Applied Overhead & Labor Policy
    """
    policy = get_manufacturing_policy(company)

    # 1. Active WIP Asset balance from GL lines
    wip_acc = resolve_manufacturing_wip_account(company)
    wip_lines = JournalEntryLine.objects.filter(
        journal_entry__company=company,
        journal_entry__status="posted",
        account=wip_acc,
    )
    wip_debits = wip_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")
    wip_credits = wip_lines.aggregate(s=Sum("credit"))["s"] or Decimal("0.00")
    active_wip_balance = (wip_debits - wip_credits).quantize(Decimal("0.01"))

    # 2. Finished Goods Inventory addition from manufacturing
    fg_acc = resolve_manufacturing_finished_goods_account(None, company)
    fg_lines = JournalEntryLine.objects.filter(
        journal_entry__company=company,
        journal_entry__source_module="manufacturing",
        journal_entry__status="posted",
        account=fg_acc,
    )
    finished_goods_value = (fg_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")).quantize(Decimal("0.01"))

    # 3. Production orders breakdown
    orders = ProductionOrder.objects.filter(recipe__product__company=company)
    total_orders_count = orders.count()

    posted_order_ids = set(
        JournalEntry.objects.filter(
            company=company,
            source_module="manufacturing",
            status="posted",
        ).values_list("source_id", flat=True)
    )

    posted_count = len(posted_order_ids)
    pending_count = orders.filter(status__in=["running", "completed"]).exclude(id__in=posted_order_ids).count()

    # 4. Scrap and waste loss from manufacturing journals
    scrap_acc = resolve_manufacturing_scrap_account(company)
    scrap_lines = JournalEntryLine.objects.filter(
        journal_entry__company=company,
        journal_entry__source_module="manufacturing",
        journal_entry__status="posted",
        account=scrap_acc,
    )
    total_scrap_value = (scrap_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")).quantize(Decimal("0.01"))

    return {
        "active_wip_balance": float(active_wip_balance),
        "finished_goods_value": float(finished_goods_value),
        "total_orders_count": total_orders_count,
        "posted_accounting_count": posted_count,
        "pending_accounting_count": pending_count,
        "total_scrap_value": float(total_scrap_value),
        "policy": {
            "enabled": policy["enabled"],
            "wip_enabled": policy["wip_enabled"],
            "labor_enabled": policy["labor_enabled"],
            "overhead_enabled": policy["overhead_enabled"],
            "scrap_enabled": policy["scrap_enabled"],
            "variance_enabled": policy["variance_enabled"],
            "labor_rate_per_unit": float(policy["labor_rate_per_unit"]),
            "overhead_rate_per_unit": float(policy["overhead_rate_per_unit"]),
        },
    }


def get_manufacturing_accounting_orders(company, status=None, accounting_status=None, search=None, page=1, page_size=20):
    """
    Returns a paginated list of production orders with accumulated costs,
    accounting status, and linked journal entries.
    """
    qs = ProductionOrder.objects.filter(
        recipe__product__company=company
    ).select_related("recipe__product", "warehouse", "line").order_by("-created_at")

    if status and status != "ALL":
        qs = qs.filter(status=status.lower())

    if search:
        qs = qs.filter(
            Q(recipe__product__name__icontains=search) |
            Q(recipe__product__sku__icontains=search) |
            Q(id__icontains=search)
        )

    # Fetch posted journal entries map
    posted_jes = {
        je.source_id: je
        for je in JournalEntry.objects.filter(
            company=company,
            source_module="manufacturing",
        ).select_related("accounting_period")
    }

    policy = get_manufacturing_policy(company)

    results = []
    for order in qs:
        je = posted_jes.get(order.id)
        if je:
            acc_status = "POSTED" if je.status == "posted" else "REVERSED"
            je_id = je.id
            je_number = je.entry_number
        elif not policy["enabled"]:
            acc_status = "NOT_REQUIRED"
            je_id = None
            je_number = None
        elif order.status in ["running", "completed"]:
            acc_status = "PENDING"
            je_id = None
            je_number = None
        else:
            acc_status = "NOT_REQUIRED"
            je_id = None
            je_number = None

        if accounting_status and accounting_status != "ALL" and acc_status != accounting_status:
            continue

        costs = calculate_production_order_costs(order, company, policy=policy)
        completed_qty = order.quantity if order.status == "completed" else 0.0

        results.append({
            "id": order.id,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "status": order.status,
            "product_id": order.recipe.product.id,
            "product_name": order.recipe.product.name,
            "sku": getattr(order.recipe.product, "sku", ""),
            "warehouse_id": order.warehouse.id,
            "warehouse_name": order.warehouse.name,
            "line_name": order.line.name if order.line else None,
            "planned_quantity": float(order.quantity),
            "completed_quantity": float(completed_qty),
            "total_material_cost": float(costs["total_material_cost"]),
            "labor_cost": float(costs["labor_cost"]),
            "overhead_cost": float(costs["overhead_cost"]),
            "total_production_cost": float(costs["total_production_cost"]),
            "unit_production_cost": float(costs["unit_production_cost"]),
            "accounting_status": acc_status,
            "journal_entry_id": je_id,
            "journal_entry_number": je_number,
        })

    # Pagination
    total = len(results)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_results = results[start:end]

    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": paginated_results,
    }
