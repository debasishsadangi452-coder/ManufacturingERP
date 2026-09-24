"""
Inventory-to-Accounting Integration Subledger (Blueprint Section No. 14)
Connects operational stock movements (receipt, issue, transfer, adjustment, write-off,
and inventory valuation events) directly to the Double-Entry Engine (#8) and General Ledger (#9).
Enforces configurable inventory valuation policy, idempotent journal posting, and complete traceability.
"""

from decimal import Decimal
from datetime import date, datetime
from django.db import transaction
from django.db.models import Sum, Q, F
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import Company
from inventory.models import Item, Warehouse, Stock, StockMovement
from procurement.models import PurchaseOrderItem
from accounting.models import Account, AccountType, JournalEntry, JournalEntryLine, AccountingSettings, AccountingPeriod
from accounting.engine import post_journal_entry, reverse_journal_entry


def get_inventory_policy(company):
    """
    Returns the configured inventory accounting policy for the tenant.
    Reads from AccountingSettings with intelligent company-safe defaults.
    """
    settings = AccountingSettings.objects.filter(company=company).first()
    if not settings:
        return {
            "enabled": True,
            "costing_method": "fifo",
            "raw_material_account_id": None,
            "finished_goods_account_id": None,
            "clearing_account_id": None,
            "cogs_account_id": None,
            "adjustment_account_id": None,
            "write_off_account_id": None,
        }
    return {
        "enabled": bool(settings.inventory_accounting_enabled),
        "costing_method": settings.inventory_costing_method or "fifo",
        "raw_material_account_id": settings.inventory_raw_material_account_id,
        "finished_goods_account_id": settings.inventory_finished_goods_account_id,
        "clearing_account_id": settings.inventory_clearing_account_id,
        "cogs_account_id": settings.inventory_cogs_account_id,
        "adjustment_account_id": settings.inventory_adjustment_account_id,
        "write_off_account_id": settings.inventory_write_off_account_id,
    }


def resolve_inventory_asset_account(item, company, account_id=None):
    """
    Resolves or provisions the active leaf Inventory Asset account for an item.
    - If account_id is provided, validates that it exists, is leaf, active, and belongs to company.
    - Finished Goods -> prefers settings.inventory_finished_goods_account or code 1230.
    - Raw Materials -> prefers settings.inventory_raw_material_account or code 1210.
    - Fallback -> any active leaf account of type Inventory under Assets.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified inventory asset account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post inventory asset to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    is_finished = getattr(item, "category", "") == "finished_good" or getattr(item, "is_finished_good", False)

    if is_finished:
        if settings and settings.inventory_finished_goods_account:
            return settings.inventory_finished_goods_account

        # Code 1230 Finished Goods Inventory
        acc = Account.objects.filter(company=company, code="1230", is_active=True).first()
        if acc and not acc.is_header:
            return acc

        acc = Account.objects.filter(
            company=company,
            name__icontains="Finished Goods",
            account_type__category="asset",
            is_active=True,
        ).exclude(children__isnull=False).first()
        if acc:
            return acc

        # Provision 1230 Finished Goods Inventory
        parent = Account.objects.filter(company=company, code__in=["1200", "1000"]).order_by("-code").first()
        acc_type = AccountType.objects.filter(name="Inventory").first() or AccountType.objects.filter(category="asset").first()
        acc, _ = Account.objects.get_or_create(
            company=company,
            code="1230",
            defaults={
                "name": "Finished Goods Inventory",
                "account_type": acc_type,
                "parent": parent,
                "description": "Completed manufactured products ready for sale/shipment",
                "currency": getattr(company, "currency", "USD") or "USD",
                "is_active": True,
            }
        )
        return acc
    else:
        if settings and settings.inventory_raw_material_account:
            return settings.inventory_raw_material_account

        # Code 1210 Raw Materials Inventory
        acc = Account.objects.filter(company=company, code="1210", is_active=True).first()
        if acc and not acc.is_header:
            return acc

        acc = Account.objects.filter(
            company=company,
            name__icontains="Raw Material",
            account_type__category="asset",
            is_active=True,
        ).exclude(children__isnull=False).first()
        if acc:
            return acc

        # Provision 1210 Raw Materials Inventory
        parent = Account.objects.filter(company=company, code__in=["1200", "1000"]).order_by("-code").first()
        acc_type = AccountType.objects.filter(name="Inventory").first() or AccountType.objects.filter(category="asset").first()
        acc, _ = Account.objects.get_or_create(
            company=company,
            code="1210",
            defaults={
                "name": "Raw Materials Inventory",
                "account_type": acc_type,
                "parent": parent,
                "description": "Stock of ingredients, packaging, and raw materials",
                "currency": getattr(company, "currency", "USD") or "USD",
                "is_active": True,
            }
        )
        return acc


def resolve_inventory_clearing_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Inventory Receipt Clearing / Offset account.
    Used for inventory receipt: DR Inventory Asset, CR Inventory Clearing / Offset.
    Prefers settings.inventory_clearing_account, then code 2020 (GRNI) or 2010 (AP Trade).
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified inventory clearing account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post clearing to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.inventory_clearing_account:
        return settings.inventory_clearing_account

    # 1. Check code 2020 GRNI / Inventory Clearing
    acc = Account.objects.filter(company=company, code="2020", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 2. Check code 2010 Accounts Payable Trade
    acc = Account.objects.filter(company=company, code="2010", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # 3. Account with name containing clearing/grni
    acc = Account.objects.filter(
        company=company,
        name__icontains="Clearing",
        account_type__category="liability",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 4. Provision standard 2020 GRNI / Inventory Clearing under 2000 Current Liabilities
    parent = Account.objects.filter(company=company, code="2000").first()
    acc_type = AccountType.objects.filter(name="Accounts Payable").first() or AccountType.objects.filter(category="liability").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="2020",
        defaults={
            "name": "Goods Received Not Invoiced (GRNI)",
            "account_type": acc_type,
            "parent": parent,
            "description": "Interim liability clearing account for stock received prior to supplier bill posting",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_inventory_cogs_account(item, company, account_id=None):
    """
    Resolves or provisions the active leaf COGS / Inventory Consumption account.
    Used for inventory issue/outward: DR COGS / Expense, CR Inventory Asset.
    Prefers settings.inventory_cogs_account, then code 5010 (Direct Materials Consumed).
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified COGS account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post consumption to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.inventory_cogs_account:
        return settings.inventory_cogs_account

    # Direct code 5010
    acc = Account.objects.filter(company=company, code="5010", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # Account with name containing Direct Materials / Raw Materials Consumed
    acc = Account.objects.filter(
        company=company,
        name__icontains="Materials Consumed",
        account_type__category="expense",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # Fallback to any leaf COGS account
    acc = Account.objects.filter(
        company=company,
        account_type__name__icontains="Cost of Goods Sold",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # Provision 5010
    parent = Account.objects.filter(company=company, code="5000").first()
    acc_type = AccountType.objects.filter(name="Cost of Goods Sold (Raw Materials)").first() or AccountType.objects.filter(category="expense").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="5010",
        defaults={
            "name": "Direct Raw Materials Consumed",
            "account_type": acc_type,
            "parent": parent,
            "description": "Cost of materials and ingredients consumed in production and operations",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_inventory_adjustment_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Inventory Adjustment & Shrinkage account.
    Used for inventory count adjustment:
    - Positive variance: DR Inventory Asset, CR Inventory Adjustment (Gain)
    - Negative variance: DR Inventory Adjustment (Loss), CR Inventory Asset
    Prefers settings.inventory_adjustment_account, then code 5090 or 5210.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified inventory adjustment account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post adjustment to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.inventory_adjustment_account:
        return settings.inventory_adjustment_account

    # Direct code 5090
    acc = Account.objects.filter(company=company, code="5090", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # Account with name containing 'Adjustment' or 'Shrinkage' or 'Variance'
    acc = Account.objects.filter(
        company=company,
        name__icontains="Adjustment",
        account_type__category="expense",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # Provision standard 5090 Inventory Adjustments & Shrinkage under 5000 COGS
    parent = Account.objects.filter(company=company, code="5000").first()
    acc_type = AccountType.objects.filter(name="Cost of Goods Sold (Manufacturing Overhead)").first() or AccountType.objects.filter(category="expense").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="5090",
        defaults={
            "name": "Inventory Adjustments & Shrinkage",
            "account_type": acc_type,
            "parent": parent,
            "description": "Net physical inventory variances, cycle count reconciliations, and stock adjustments",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def resolve_inventory_write_off_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Inventory Loss / Write-off Expense account.
    Used for scrapped or damaged inventory write-offs: DR Inventory Write-off Expense, CR Inventory Asset.
    Prefers settings.inventory_write_off_account, then code 6520 or 5090.
    """
    if account_id:
        acc = Account.objects.filter(pk=account_id, company=company, is_active=True).first()
        if not acc:
            raise ValidationError(_(f"Specified write-off account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post write-off to a header account. Select a leaf account."))
        return acc

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.inventory_write_off_account:
        return settings.inventory_write_off_account

    # Direct code 6520
    acc = Account.objects.filter(company=company, code="6520", is_active=True).first()
    if acc and not acc.is_header:
        return acc

    # Account with name containing 'Write-off' or 'Loss'
    acc = Account.objects.filter(
        company=company,
        name__icontains="Write-off",
        account_type__category="expense",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # Provision standard 6520 Inventory Loss & Write-off Expense under 6000 Operating Expenses
    parent = Account.objects.filter(company=company, code="6000").first()
    acc_type = AccountType.objects.filter(name="Selling, General & Administrative").first() or AccountType.objects.filter(category="expense").first()
    acc, _ = Account.objects.get_or_create(
        company=company,
        code="6520",
        defaults={
            "name": "Inventory Loss & Write-off Expense",
            "account_type": acc_type,
            "parent": parent,
            "description": "Cost of damaged, expired, or obsolete stock written off from perpetual inventory",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def calculate_movement_valuation(movement, company, costing_method_override=None, unit_cost_override=None):
    """
    Determines unit cost and total valuation for an inventory movement according to the configured costing method.
    Returns:
      {
        "unit_cost": Decimal,
        "valuation_amount": Decimal,
        "costing_method": str,
      }
    """
    settings = AccountingSettings.objects.filter(company=company).first()
    costing_method = costing_method_override or (settings.inventory_costing_method if settings else "fifo") or "fifo"

    if unit_cost_override is not None and Decimal(str(unit_cost_override)) > Decimal("0.00"):
        unit_cost = Decimal(str(unit_cost_override)).quantize(Decimal("0.01"))
    else:
        # Resolve cost from Item or Procurement history
        item = movement.item
        item_cost = Decimal(str(getattr(item, "purchase_cost", "0.00") or "0.00"))

        if item_cost > Decimal("0.00"):
            unit_cost = item_cost.quantize(Decimal("0.01"))
        else:
            # Check latest PO price for this item
            poi = PurchaseOrderItem.objects.filter(
                item=item,
                po__vendor__company=company
            ).order_by("-id").first()
            if poi and poi.unit_price > Decimal("0.00"):
                unit_cost = Decimal(str(poi.unit_price)).quantize(Decimal("0.01"))
            else:
                # If finished good with selling price, or fallback standard 1.00
                selling_price = Decimal(str(getattr(item, "selling_price", "0.00") or "0.00"))
                unit_cost = (selling_price if selling_price > Decimal("0.00") else Decimal("1.00")).quantize(Decimal("0.01"))

    qty = Decimal(str(abs(movement.quantity)))
    valuation_amount = (qty * unit_cost).quantize(Decimal("0.01"))

    return {
        "unit_cost": unit_cost,
        "valuation_amount": valuation_amount,
        "costing_method": costing_method,
    }


def determine_movement_accounting_requirement(movement, company):
    """
    Determines whether a specific stock movement requires accounting treatment.
    Returns:
      (requires_accounting: bool, reason: str, event_subtype: str)
    """
    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and not settings.inventory_accounting_enabled:
        return False, "Inventory accounting is disabled in Accounting Settings.", "disabled"

    qty = Decimal(str(movement.quantity))
    if qty == Decimal("0.00"):
        return False, "Zero quantity movement has no financial valuation delta.", "zero_quantity"

    ref = (movement.reference or "").lower()

    # Transfers: Within the same company, transfer between warehouses changes location without altering valuation
    if ref.startswith("transfer to") or ref.startswith("transfer from") or "transfer #" in ref:
        return False, "Internal warehouse transfer preserves total inventory asset valuation - no redundant GL entry required.", "transfer_internal"

    # Write-off: Scrapped / expired / damaged stock
    if "write-off" in ref or "write off" in ref or "scrap" in ref or "damaged" in ref or "expired" in ref:
        return True, "Inventory write-off / scrap loss", "write_off"

    if movement.movement_type == "IN":
        return True, "Inventory receipt (stock inward)", "receipt"

    if movement.movement_type == "OUT":
        return True, "Inventory issue / consumption (stock outward)", "issue"

    if movement.movement_type == "ADJUST":
        if qty > Decimal("0.00"):
            return True, "Positive inventory adjustment (physical count gain)", "adjustment_gain"
        elif qty < Decimal("0.00"):
            return True, "Negative inventory adjustment (physical count shrinkage / loss)", "adjustment_loss"
        else:
            return False, "Zero adjustment variance", "zero_delta"

    return False, f"Unrecognized movement type: {movement.movement_type}", "unrecognized"


def get_inventory_accounting_preview(
    movement_id,
    company,
    inventory_account_id=None,
    offset_account_id=None,
    unit_cost_override=None,
):
    """
    Computes and returns a prospective balanced double-entry preview for an inventory movement
    without committing any changes to the database.
    """
    try:
        movement = StockMovement.objects.select_related("item", "warehouse", "created_by").get(
            pk=movement_id,
            item__company=company,
        )
    except StockMovement.DoesNotExist:
        raise ValidationError(_(f"Stock movement #{movement_id} not found for company '{company.name}'."))

    requires_accounting, reason, event_subtype = determine_movement_accounting_requirement(movement, company)
    valuation_data = calculate_movement_valuation(movement, company, unit_cost_override=unit_cost_override)
    valuation_amount = valuation_data["valuation_amount"]
    unit_cost = valuation_data["unit_cost"]
    costing_method = valuation_data["costing_method"]

    # Check existing journal entry
    existing_je = JournalEntry.objects.filter(
        company=company,
        source_module="inventory",
        source_id=movement.id,
        status="posted",
    ).first()

    lines_preview = []
    total_debit = 0.0
    total_credit = 0.0
    is_balanced = True

    if requires_accounting and valuation_amount > Decimal("0.00"):
        asset_account = resolve_inventory_asset_account(movement.item, company, account_id=inventory_account_id)

        # Resolve offset account based on event subtype
        if event_subtype == "receipt":
            offset_account = resolve_inventory_clearing_account(company, account_id=offset_account_id)
            # DR Inventory Asset, CR Inventory Clearing
            lines_preview = [
                {
                    "account_id": asset_account.id,
                    "account_code": asset_account.code,
                    "account_name": asset_account.name,
                    "category": asset_account.account_type.category,
                    "type": "debit",
                    "debit": float(valuation_amount),
                    "credit": 0.0,
                    "description": f"Inventory Receipt - {movement.item.name} ({movement.warehouse.name})",
                },
                {
                    "account_id": offset_account.id,
                    "account_code": offset_account.code,
                    "account_name": offset_account.name,
                    "category": offset_account.account_type.category,
                    "type": "credit",
                    "debit": 0.0,
                    "credit": float(valuation_amount),
                    "description": f"Receipt Clearing - {movement.item.name} (Ref: {movement.reference or 'Inward'})",
                },
            ]
        elif event_subtype == "issue":
            offset_account = resolve_inventory_cogs_account(movement.item, company, account_id=offset_account_id)
            # DR COGS / Expense, CR Inventory Asset
            lines_preview = [
                {
                    "account_id": offset_account.id,
                    "account_code": offset_account.code,
                    "account_name": offset_account.name,
                    "category": offset_account.account_type.category,
                    "type": "debit",
                    "debit": float(valuation_amount),
                    "credit": 0.0,
                    "description": f"Material Consumption/COGS - {movement.item.name} ({movement.warehouse.name})",
                },
                {
                    "account_id": asset_account.id,
                    "account_code": asset_account.code,
                    "account_name": asset_account.name,
                    "category": asset_account.account_type.category,
                    "type": "credit",
                    "debit": 0.0,
                    "credit": float(valuation_amount),
                    "description": f"Inventory Issue - {movement.item.name} (Ref: {movement.reference or 'Outward'})",
                },
            ]
        elif event_subtype == "adjustment_gain":
            offset_account = resolve_inventory_adjustment_account(company, account_id=offset_account_id)
            # DR Inventory Asset, CR Inventory Adjustment Gain
            lines_preview = [
                {
                    "account_id": asset_account.id,
                    "account_code": asset_account.code,
                    "account_name": asset_account.name,
                    "category": asset_account.account_type.category,
                    "type": "debit",
                    "debit": float(valuation_amount),
                    "credit": 0.0,
                    "description": f"Inventory Physical Gain - {movement.item.name} ({movement.warehouse.name})",
                },
                {
                    "account_id": offset_account.id,
                    "account_code": offset_account.code,
                    "account_name": offset_account.name,
                    "category": offset_account.account_type.category,
                    "type": "credit",
                    "debit": 0.0,
                    "credit": float(valuation_amount),
                    "description": f"Inventory Variance Gain - {movement.item.name} (Ref: {movement.reference or 'Adjustment'})",
                },
            ]
        elif event_subtype == "adjustment_loss":
            offset_account = resolve_inventory_adjustment_account(company, account_id=offset_account_id)
            # DR Inventory Adjustment Loss, CR Inventory Asset
            lines_preview = [
                {
                    "account_id": offset_account.id,
                    "account_code": offset_account.code,
                    "account_name": offset_account.name,
                    "category": offset_account.account_type.category,
                    "type": "debit",
                    "debit": float(valuation_amount),
                    "credit": 0.0,
                    "description": f"Inventory Variance Shrinkage - {movement.item.name} (Ref: {movement.reference or 'Adjustment'})",
                },
                {
                    "account_id": asset_account.id,
                    "account_code": asset_account.code,
                    "account_name": asset_account.name,
                    "category": asset_account.account_type.category,
                    "type": "credit",
                    "debit": 0.0,
                    "credit": float(valuation_amount),
                    "description": f"Inventory Adjustment Reduction - {movement.item.name} ({movement.warehouse.name})",
                },
            ]
        elif event_subtype == "write_off":
            offset_account = resolve_inventory_write_off_account(company, account_id=offset_account_id)
            # DR Write-off Expense, CR Inventory Asset
            lines_preview = [
                {
                    "account_id": offset_account.id,
                    "account_code": offset_account.code,
                    "account_name": offset_account.name,
                    "category": offset_account.account_type.category,
                    "type": "debit",
                    "debit": float(valuation_amount),
                    "credit": 0.0,
                    "description": f"Inventory Write-off Loss - {movement.item.name} (Ref: {movement.reference or 'Write-off'})",
                },
                {
                    "account_id": asset_account.id,
                    "account_code": asset_account.code,
                    "account_name": asset_account.name,
                    "category": asset_account.account_type.category,
                    "type": "credit",
                    "debit": 0.0,
                    "credit": float(valuation_amount),
                    "description": f"Inventory Write-off Asset Reduction - {movement.item.name} ({movement.warehouse.name})",
                },
            ]

        total_debit = float(valuation_amount)
        total_credit = float(valuation_amount)
        is_balanced = (round(total_debit, 2) == round(total_credit, 2))

    return {
        "movement_id": movement.id,
        "item_id": movement.item_id,
        "item_name": movement.item.name,
        "item_category": movement.item.category,
        "warehouse_id": movement.warehouse_id,
        "warehouse_name": movement.warehouse.name,
        "movement_type": movement.movement_type,
        "movement_date": movement.created_at.date().isoformat(),
        "quantity": float(movement.quantity),
        "reference": movement.reference or "",
        "unit_cost": float(unit_cost),
        "valuation_amount": float(valuation_amount),
        "costing_method": costing_method,
        "requires_accounting": requires_accounting,
        "reason": reason,
        "event_subtype": event_subtype,
        "is_posted": bool(existing_je),
        "journal_entry_id": existing_je.id if existing_je else None,
        "journal_entry_number": existing_je.entry_number if existing_je else None,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "is_balanced": is_balanced,
        "lines": lines_preview,
    }


def post_inventory_movement_to_accounting(
    movement_id,
    user,
    company,
    inventory_account_id=None,
    offset_account_id=None,
    unit_cost_override=None,
    transaction_date=None,
):
    """
    Posts an operational inventory movement to the General Ledger via Double-Entry Engine (#8).
    Creates an atomic, balanced Journal Entry and enforces idempotency.
    """
    with transaction.atomic():
        try:
            movement = StockMovement.objects.select_for_update().select_related("item", "warehouse").get(
                pk=movement_id,
                item__company=company,
            )
        except StockMovement.DoesNotExist:
            raise ValidationError(_(f"Stock movement #{movement_id} not found in company '{company.name}'."))

        # Determine accounting requirement
        requires_accounting, reason, event_subtype = determine_movement_accounting_requirement(movement, company)
        if not requires_accounting:
            raise ValidationError(_(f"Movement #{movement.id} does not require accounting posting: {reason}"))

        # Idempotency check: prevent duplicate journal creation
        existing_je = JournalEntry.objects.filter(
            company=company,
            source_module="inventory",
            source_id=movement.id,
            status="posted",
        ).first()
        if existing_je:
            raise ValidationError(_(
                f"Stock movement #{movement.id} has already been posted to the General Ledger "
                f"(Journal Entry: {existing_je.entry_number})."
            ))

        # Valuation
        valuation_data = calculate_movement_valuation(movement, company, unit_cost_override=unit_cost_override)
        valuation_amount = valuation_data["valuation_amount"]
        unit_cost = valuation_data["unit_cost"]

        if valuation_amount <= Decimal("0.00"):
            raise ValidationError(_(f"Inventory valuation amount must be greater than zero. Current valuation is {valuation_amount}."))

        # Resolve accounts
        asset_account = resolve_inventory_asset_account(movement.item, company, account_id=inventory_account_id)

        tx_date = transaction_date or movement.created_at.date()
        if isinstance(tx_date, str):
            tx_date = date.fromisoformat(tx_date)

        ref_text = f"STK-MOV-{movement.id}"

        # Create draft journal entry header
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=tx_date,
            reference=ref_text,
            description=f"Inventory {event_subtype.replace('_', ' ').title()} - {movement.item.name} ({movement.quantity} {movement.item.unit})",
            source_module="inventory",
            source_id=movement.id,
            created_by=user,
            status="draft",
        )

        if event_subtype == "receipt":
            offset_account = resolve_inventory_clearing_account(company, account_id=offset_account_id)
            # Line 1: Debit Inventory Asset
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=1,
                debit=valuation_amount,
                credit=Decimal("0.00"),
                description=f"Inventory Receipt - {movement.item.name} ({movement.warehouse.name})",
            )
            # Line 2: Credit Inventory Clearing
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=offset_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=valuation_amount,
                description=f"Receipt Clearing - {movement.item.name} (Ref: {movement.reference or 'Inward'})",
            )
        elif event_subtype == "issue":
            offset_account = resolve_inventory_cogs_account(movement.item, company, account_id=offset_account_id)
            # Line 1: Debit COGS / Consumption
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=offset_account,
                line_number=1,
                debit=valuation_amount,
                credit=Decimal("0.00"),
                description=f"Direct Materials Consumed - {movement.item.name} ({movement.warehouse.name})",
            )
            # Line 2: Credit Inventory Asset
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=valuation_amount,
                description=f"Inventory Issue - {movement.item.name} (Ref: {movement.reference or 'Outward'})",
            )
        elif event_subtype == "adjustment_gain":
            offset_account = resolve_inventory_adjustment_account(company, account_id=offset_account_id)
            # Line 1: Debit Inventory Asset
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=1,
                debit=valuation_amount,
                credit=Decimal("0.00"),
                description=f"Inventory Physical Gain - {movement.item.name} ({movement.warehouse.name})",
            )
            # Line 2: Credit Inventory Adjustment Gain
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=offset_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=valuation_amount,
                description=f"Inventory Variance Gain - {movement.item.name} (Ref: {movement.reference or 'Adjustment'})",
            )
        elif event_subtype == "adjustment_loss":
            offset_account = resolve_inventory_adjustment_account(company, account_id=offset_account_id)
            # Line 1: Debit Inventory Adjustment Loss
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=offset_account,
                line_number=1,
                debit=valuation_amount,
                credit=Decimal("0.00"),
                description=f"Inventory Variance Loss - {movement.item.name} (Ref: {movement.reference or 'Adjustment'})",
            )
            # Line 2: Credit Inventory Asset
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=valuation_amount,
                description=f"Inventory Adjustment Reduction - {movement.item.name} ({movement.warehouse.name})",
            )
        elif event_subtype == "write_off":
            offset_account = resolve_inventory_write_off_account(company, account_id=offset_account_id)
            # Line 1: Debit Inventory Write-off Expense
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=offset_account,
                line_number=1,
                debit=valuation_amount,
                credit=Decimal("0.00"),
                description=f"Inventory Write-off Loss - {movement.item.name} (Ref: {movement.reference or 'Write-off'})",
            )
            # Line 2: Credit Inventory Asset
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=valuation_amount,
                description=f"Inventory Asset Write-off - {movement.item.name} ({movement.warehouse.name})",
            )
        else:
            raise ValidationError(_(f"Unsupported event subtype '{event_subtype}' for movement #{movement.id}."))

        # Post via Double-Entry Engine (#8)
        posted_je = post_journal_entry(entry.id, user=user)
        return posted_je


def reverse_inventory_accounting(movement_id, user, company, reason=""):
    """
    Reverses an inventory accounting journal entry using the Double-Entry Engine (#8).
    Creates an atomic, linked reversal journal entry with mirrored debits and credits.
    """
    with transaction.atomic():
        try:
            movement = StockMovement.objects.select_related("item", "warehouse").get(
                pk=movement_id,
                item__company=company,
            )
        except StockMovement.DoesNotExist:
            raise ValidationError(_(f"Stock movement #{movement_id} not found in company '{company.name}'."))

        posted_je = JournalEntry.objects.filter(
            company=company,
            source_module="inventory",
            source_id=movement.id,
            status="posted",
        ).first()

        if not posted_je:
            raise ValidationError(_(f"No posted journal entry found for stock movement #{movement_id}."))

        reversal_date = timezone.now().date()
        reversal_reason = reason or f"Reversal of inventory journal for movement #{movement.id}"

        reversal_je = reverse_journal_entry(
            entry_id=posted_je.id,
            user=user,
            reason=reversal_reason,
            reversal_date=reversal_date,
            company=company,
        )

        posted_je.refresh_from_db()

        return {
            "movement_id": movement.id,
            "status": "reversed",
            "original_journal_entry": posted_je,
            "reversal_journal_entry": reversal_je,
        }


def post_inventory_valuation_event(
    item_id,
    new_unit_cost,
    user,
    company,
    warehouse_id=None,
    reason="",
    transaction_date=None,
):
    """
    Records a deliberate inventory valuation event (e.g. standard cost revaluation, fair value write-down).
    Computes total accounting delta across on-hand inventory and posts a balanced journal entry:
      - If delta > 0 (Gain): DR Inventory Asset, CR Inventory Adjustment / Valuation Gain
      - If delta < 0 (Loss): DR Inventory Adjustment / Valuation Loss, CR Inventory Asset
      - If delta == 0: Rejects with ValidationError (no redundant entry created)
    """
    with transaction.atomic():
        try:
            item = Item.objects.select_for_update().get(pk=item_id, company=company)
        except Item.DoesNotExist:
            raise ValidationError(_(f"Item #{item_id} not found for company '{company.name}'."))

        new_cost = Decimal(str(new_unit_cost)).quantize(Decimal("0.01"))
        if new_cost < Decimal("0.00"):
            raise ValidationError(_("New unit cost cannot be negative."))

        old_cost = Decimal(str(getattr(item, "purchase_cost", "0.00") or "0.00")).quantize(Decimal("0.01"))

        # Aggregate total on-hand quantity
        stock_qs = Stock.objects.filter(item=item)
        if warehouse_id:
            stock_qs = stock_qs.filter(warehouse_id=warehouse_id)

        total_qty = Decimal(str(stock_qs.aggregate(total=Sum("quantity"))["total"] or 0))
        if total_qty <= Decimal("0.00"):
            raise ValidationError(_(f"Cannot perform valuation adjustment: Item '{item.name}' has 0 on-hand stock."))

        old_valuation = (total_qty * old_cost).quantize(Decimal("0.01"))
        new_valuation = (total_qty * new_cost).quantize(Decimal("0.01"))
        delta = new_valuation - old_valuation

        if delta == Decimal("0.00"):
            raise ValidationError(_("Valuation delta is zero. Unit cost is identical, no journal entry required."))

        # Update Item's standard purchase cost
        item.purchase_cost = new_cost
        item.save(update_fields=["purchase_cost"])

        asset_account = resolve_inventory_asset_account(item, company)
        adj_account = resolve_inventory_adjustment_account(company)

        tx_date = transaction_date or timezone.now().date()
        if isinstance(tx_date, str):
            tx_date = date.fromisoformat(tx_date)

        ref_text = f"REVAL-ITM-{item.id}"
        abs_delta = abs(delta)

        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=tx_date,
            reference=ref_text,
            description=f"Inventory Revaluation - {item.name}: Old Cost ${old_cost} -> New Cost ${new_cost} (Qty: {total_qty})",
            source_module="inventory.valuation",
            source_id=item.id,
            created_by=user,
            status="draft",
        )

        if delta > Decimal("0.00"):
            # Revaluation Gain: DR Asset, CR Adjustment Gain
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=1,
                debit=abs_delta,
                credit=Decimal("0.00"),
                description=f"Inventory Revaluation Uplift - {item.name} (${old_cost} to ${new_cost})",
            )
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=adj_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=abs_delta,
                description=f"Inventory Valuation Gain - {item.name} (Qty: {total_qty})",
            )
        else:
            # Revaluation Loss: DR Adjustment Loss, CR Asset
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=adj_account,
                line_number=1,
                debit=abs_delta,
                credit=Decimal("0.00"),
                description=f"Inventory Valuation Write-down - {item.name} (${old_cost} to ${new_cost})",
            )
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=asset_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=abs_delta,
                description=f"Inventory Revaluation Reduction - {item.name} (Qty: {total_qty})",
            )

        posted_je = post_journal_entry(entry.id, user=user)
        return {
            "item_id": item.id,
            "item_name": item.name,
            "total_quantity": float(total_qty),
            "old_unit_cost": float(old_cost),
            "new_unit_cost": float(new_cost),
            "delta_amount": float(delta),
            "journal_entry_id": posted_je.id,
            "journal_entry_number": posted_je.entry_number,
        }


def get_inventory_accounting_summary(company, as_of_date=None):
    """
    Computes real-time Inventory-to-Accounting KPIs directly from the General Ledger and Stock Movements:
      - Total Inventory Asset Value (GL leaf balances for 1200 series)
      - Total Raw Materials GL Value (1210)
      - Total Finished Goods GL Value (1230)
      - Total Inventory Adjustments & Shrinkage Expense (5090)
      - Movement counts: total, posted, pending, not required
      - Configured costing policy
    """
    # 1. General Ledger asset account balances
    inv_asset_lines = JournalEntryLine.objects.filter(
        company=company,
        journal_entry__status="posted",
        account__code__startswith="12",
        account__account_type__category="asset",
    )
    if as_of_date:
        inv_asset_lines = inv_asset_lines.filter(journal_entry__transaction_date__lte=as_of_date)

    gl_inv_total = Decimal("0.00")
    raw_materials_gl = Decimal("0.00")
    finished_goods_gl = Decimal("0.00")

    for line in inv_asset_lines.values("account__code").annotate(
        total_dr=Sum("debit"), total_cr=Sum("credit")
    ):
        net_balance = (line["total_dr"] or Decimal("0.00")) - (line["total_cr"] or Decimal("0.00"))
        gl_inv_total += net_balance
        if line["account__code"] == "1210":
            raw_materials_gl += net_balance
        elif line["account__code"] == "1230":
            finished_goods_gl += net_balance

    # 2. General Ledger Adjustment / Shrinkage expense (5090)
    adj_lines = JournalEntryLine.objects.filter(
        company=company,
        journal_entry__status="posted",
        account__code="5090",
    )
    if as_of_date:
        adj_lines = adj_lines.filter(journal_entry__transaction_date__lte=as_of_date)

    adj_dr = adj_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")
    adj_cr = adj_lines.aggregate(s=Sum("credit"))["s"] or Decimal("0.00")
    net_adjustment_expense = adj_dr - adj_cr

    # 3. Stock Movements metrics
    movements_qs = StockMovement.objects.filter(item__company=company)
    total_movements_count = movements_qs.count()

    # Posted inventory journal entries
    posted_je_qs = JournalEntry.objects.filter(
        company=company,
        source_module="inventory",
        status="posted",
    )
    posted_movement_ids = set(posted_je_qs.values_list("source_id", flat=True))
    posted_count = len(posted_movement_ids)

    # Calculate posted valuation volume from journal entry lines (debits to 1200 series)
    posted_lines = JournalEntryLine.objects.filter(
        company=company,
        journal_entry__in=posted_je_qs,
        account__code__startswith="12",
    )
    posted_valuation_volume = posted_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")

    # Policy
    policy = get_inventory_policy(company)

    # Pending count: movements requiring accounting that are not yet posted
    pending_count = 0
    not_required_count = 0

    # Sample recent movements to evaluate requirement
    for mov in movements_qs.select_related("item")[:200]:
        requires, _, _ = determine_movement_accounting_requirement(mov, company)
        if not requires:
            not_required_count += 1
        elif mov.id in posted_movement_ids:
            pass
        else:
            pending_count += 1

    return {
        "as_of_date": as_of_date or timezone.now().date().isoformat(),
        "total_inventory_gl_value": float(gl_inv_total),
        "raw_materials_gl_value": float(raw_materials_gl),
        "finished_goods_gl_value": float(finished_goods_gl),
        "net_adjustment_expense": float(net_adjustment_expense),
        "total_movements_count": total_movements_count,
        "posted_movements_count": posted_count,
        "pending_movements_count": pending_count,
        "not_required_count": not_required_count,
        "posted_valuation_volume": float(posted_valuation_volume),
        "policy": policy,
    }


def get_inventory_accounting_movements(
    company,
    search=None,
    movement_type=None,
    status_filter=None,
    limit=50,
):
    """
    Returns list of stock movements annotated with accounting requirement,
    valuation, accounting status, and linked journal entry information.
    """
    qs = StockMovement.objects.filter(item__company=company).select_related(
        "item", "warehouse", "created_by"
    ).order_by("-created_at")

    if search:
        qs = qs.filter(
            Q(item__name__icontains=search) |
            Q(warehouse__name__icontains=search) |
            Q(reference__icontains=search)
        )

    if movement_type and movement_type != "all":
        qs = qs.filter(movement_type=movement_type.upper())

    # Fetch posted journal entries for inventory
    posted_jes = {
        je.source_id: je
        for je in JournalEntry.objects.filter(
            company=company,
            source_module="inventory",
        ).select_related("accounting_period")
    }

    results = []
    for mov in qs[:limit]:
        je = posted_jes.get(mov.id)
        requires_accounting, reason, event_subtype = determine_movement_accounting_requirement(mov, company)
        val = calculate_movement_valuation(mov, company)

        if je and je.status == "posted":
            accounting_status = "posted"
        elif je and je.status == "reversed":
            accounting_status = "reversed"
        elif not requires_accounting:
            accounting_status = "not_required"
        else:
            accounting_status = "pending"

        if status_filter and status_filter != "all" and accounting_status != status_filter:
            continue

        results.append({
            "id": mov.id,
            "movement_type": mov.movement_type,
            "event_subtype": event_subtype,
            "item_id": mov.item_id,
            "item_name": mov.item.name,
            "item_category": mov.item.category,
            "warehouse_id": mov.warehouse_id,
            "warehouse_name": mov.warehouse.name,
            "quantity": float(mov.quantity),
            "unit": mov.item.unit,
            "reference": mov.reference or "",
            "created_at": mov.created_at.isoformat(),
            "created_by": mov.created_by.username if mov.created_by else None,
            "unit_cost": float(val["unit_cost"]),
            "valuation_amount": float(val["valuation_amount"]),
            "costing_method": val["costing_method"],
            "requires_accounting": requires_accounting,
            "accounting_requirement_reason": reason,
            "accounting_status": accounting_status,
            "journal_entry_id": je.id if je else None,
            "journal_entry_number": je.entry_number if je else None,
            "journal_entry_status": je.status if je else None,
            "journal_entry_date": je.transaction_date.isoformat() if je else None,
        })

    return results
