from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from production.models import ProductionOrder
from quality.models import QualityCheck
from inventory.models import Stock, InventoryRequest, Item
from sales.models import SalesOrder
from procurement.models import PurchaseOrder, GoodsReceipt
from finance.models import ExpenseRequest
from maintenance.models import MaintenanceTask, Equipment
from core.models import Notification, DataChangeEvent


# ── Helpers ───────────────────────────────────────────────────────────────────

def _notify(role, message, related_id=None, related_type="", company=None, module="general"):
    """File a notification for `role` under `module`.

    `company` must be supplied or the row is invisible to every tenant — the
    viewset filters on the caller's company. Use the _company_of_* resolvers
    below rather than passing it by hand.
    """
    Notification.objects.create(
        recipient_role=role,
        message=message,
        related_id=related_id,
        related_type=related_type,
        company=company,
        module=module,
    )


def _safe(obj, *path):
    """Walk a relation path, returning None if any hop is missing.

    Signals fire on partially-built graphs and on legacy rows whose company is
    null, so every hop here is optional by design.
    """
    for attr in path:
        if obj is None:
            return None
        obj = getattr(obj, attr, None)
    return obj


def _company_of_production_order(order):
    return _safe(order, 'recipe', 'product', 'company')


def _company_of_sales_order(order):
    return _safe(order, 'customer', 'company')


def _company_of_item(item):
    return _safe(item, 'company')


def _push(model_name, instance, action, payload, visible_to):
    DataChangeEvent.objects.create(
        model_name=model_name,
        record_id=instance.pk,
        action=action,
        payload=payload,
        visible_to=visible_to,
    )


def _serialize(instance, fields):
    """Turn a model instance into a JSON-safe dict for the given field names."""
    result = {'id': instance.pk}
    for field in fields:
        val = getattr(instance, field, None)
        if val is None:
            result[field] = None
        elif hasattr(val, 'pk'):           # FK / related object loaded
            result[f'{field}_id'] = val.pk
            result[f'{field}_str'] = str(val)
        elif hasattr(val, 'isoformat'):    # datetime / date
            result[field] = val.isoformat()
        elif isinstance(val, (int, float, bool, str)):
            result[field] = val
        else:
            result[field] = str(val)
    return result


# ── Inventory ─────────────────────────────────────────────────────────────────

INVENTORY_ROLES = ['store', 'production', 'admin']


@receiver(post_save, sender=Item)
def on_item_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['name', 'category', 'unit'])
    _push('Item', instance, 'created' if created else 'updated', payload, INVENTORY_ROLES)


@receiver(post_save, sender=Stock)
def on_stock_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['item', 'warehouse', 'quantity'])
    _push('Stock', instance, 'created' if created else 'updated', payload, INVENTORY_ROLES)

    company = _company_of_item(instance.item)

    if instance.quantity < 50 and instance.item.category == 'raw_material':
        # Scope the duplicate check to the tenant — otherwise one company's open
        # alert suppresses the same alert for every other company.
        if not Notification.objects.filter(
            company=company,
            message__contains=f'Low stock: {instance.item.name}',
            is_read=False,
        ).exists():
            _notify('store', f'Low stock: {instance.item.name} ({instance.quantity} {instance.item.unit} left). Consider procurement.', instance.item.id, 'Item', company, 'inventory')
            _notify('admin', f'Low stock alert: {instance.item.name} has only {instance.quantity} {instance.item.unit} remaining.', instance.item.id, 'Item', company, 'inventory')


@receiver(post_save, sender=InventoryRequest)
def on_inventory_request_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['item', 'warehouse', 'quantity', 'status', 'created_at'])
    _push('InventoryRequest', instance, 'created' if created else 'updated', payload, INVENTORY_ROLES)

    company = _company_of_item(instance.item)

    # A goods receipt releases every request on the PO at once. Notifying per
    # request would give production one message per material; the receipt
    # emits a single summary per production order instead.
    if getattr(instance, '_suppress_supply_notification', False):
        return

    if created:
        # Step 3 of the cycle: production is short of raw material, so inventory
        # is told to procure it. Filed under Inventory — that is where store acts.
        _notify('store', f'New inventory request: {instance.quantity} {instance.item.name} needed at {instance.warehouse.name}.', instance.id, 'InventoryRequest', company, 'inventory')
        _notify('admin', f'Inventory request #{instance.id}: {instance.quantity} {instance.item.name} — status: {instance.status}.', instance.id, 'InventoryRequest', company, 'inventory')
    elif instance.status == 'supplied':
        # Step 4: material has landed, so production can resume. Filed under
        # Production — production acts on its own page, not on Inventory's.
        # :g trims float noise (11850.10066416 → 11850.1) and the unit is named,
        # so the message reads as a quantity rather than a raw number.
        qty = f'{instance.quantity:g} {instance.item.unit}'
        prod_id = instance.production_order_id
        for_what = f' (Production Order #{prod_id})' if prod_id else ''
        _notify('production', f'Material received: {qty} of {instance.item.name} delivered to {instance.warehouse.name}{for_what} — production can resume.', prod_id or instance.id, 'ProductionOrder' if prod_id else 'InventoryRequest', company, 'production')
        _notify('admin', f'Inventory request #{instance.id} fulfilled — {qty} of {instance.item.name} supplied to production.', instance.id, 'InventoryRequest', company, 'inventory')


# ── Production ────────────────────────────────────────────────────────────────

PRODUCTION_ROLES = ['production', 'admin']


@receiver(post_save, sender=ProductionOrder)
def on_production_order_change(sender, instance, created, **kwargs):
    try:
        payload = _serialize(instance, ['recipe', 'warehouse', 'quantity', 'status'])
        _push('ProductionOrder', instance, 'created' if created else 'updated', payload, PRODUCTION_ROLES)

        company = _company_of_production_order(instance)

        # Materials that were reserved up front (by mark_ready_for_production)
        # are already deducted from stock, so a shortage check here would see
        # the depleted balance and re-request material that is in hand.
        if created and not instance.materials_reserved:
            _notify('admin', f'New Production Order #{instance.id} created for {instance.quantity} units.', instance.id, 'ProductionOrder', company, 'production')

            shortages = []
            # Ingredient quantities are per batch — scale by whole batches.
            for ing, required in instance.recipe.material_requirements(instance.quantity):
                stock = Stock.objects.filter(item=ing.item, warehouse=instance.warehouse).first()
                current_qty = stock.quantity if stock else 0
                if current_qty < required:
                    shortages.append(ing.item.name)
                    InventoryRequest.objects.get_or_create(
                        item=ing.item,
                        warehouse=instance.warehouse,
                        production_order=instance,
                        defaults={'quantity': required - current_qty, 'status': 'pending'},
                    )

            if shortages:
                # Step 3: production is blocked on raw material. Store is told on
                # the Inventory page; production keeps its own copy on Production
                # so the blocked batch is visible where the work is managed.
                _notify('store', f'Material shortage for Production #{instance.id}: {", ".join(shortages)}. Inventory request created.', instance.id, 'ProductionOrder', company, 'inventory')
                _notify('production', f'Production #{instance.id} is short of: {", ".join(shortages)}. Awaiting inventory.', instance.id, 'ProductionOrder', company, 'production')
        elif created:
            _notify('admin', f'New Production Order #{instance.id} created for {instance.quantity} units.', instance.id, 'ProductionOrder', company, 'production')

        if not created and instance.status == 'completed':
            # The batch is made, so any material request it raised is moot.
            # Without this they sit "pending" forever and clutter the store's
            # queue with shortages nobody needs to act on any more.
            InventoryRequest.objects.filter(
                production_order=instance, status__in=['pending', 'procuring']
            ).update(status='cancelled')

            # quality/signals.py also listens for completion and creates the
            # check. Whichever handler runs first wins the create; the loser
            # must still notify, so the notification is deliberately NOT nested
            # inside the create branch — that nesting is why quality was never
            # told a batch was waiting.
            if not QualityCheck.objects.filter(production_order=instance).exists():
                QualityCheck.objects.create(
                    production_order=instance,
                    status='pending',
                    test_type='Post-Production',
                    parameter='Visual & Weight',
                )

            # Step 5: the batch leaves production and lands in quality's queue.
            # Guarded so a re-save of an already-completed order does not spam.
            already_notified = Notification.objects.filter(
                company=company, module='quality', recipient_role='quality',
                related_type='ProductionOrder', related_id=instance.id,
            ).exists()
            if not already_notified:
                _notify('quality', f'Production Order #{instance.id} completed — batch requires quality inspection.', instance.id, 'ProductionOrder', company, 'quality')
                _notify('production', f'Production Order #{instance.id} completed — sent to quality for approval.', instance.id, 'ProductionOrder', company, 'production')
                _notify('admin', f'Production Order #{instance.id} completed and sent to quality.', instance.id, 'ProductionOrder', company, 'production')
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'Signal error on ProductionOrder #{instance.pk}: {e}', exc_info=True)


# ── Quality ───────────────────────────────────────────────────────────────────

QUALITY_ROLES = ['quality', 'production', 'admin']


@receiver(post_save, sender=QualityCheck)
def on_quality_check_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['production_order', 'status', 'test_type', 'parameter', 'result'])
    _push('QualityCheck', instance, 'created' if created else 'updated', payload, QUALITY_ROLES)

    order = instance.production_order
    company = _company_of_production_order(order)
    # Set when this batch was raised to fill a customer order, null for
    # make-to-stock. It decides whether sales has anyone to notify.
    sales_order = getattr(order, 'sales_order', None)
    product = _safe(order, 'recipe', 'product', 'name') or 'batch'

    if instance.status == 'approved':
        # Step 7a: quality passed. Sales is the party waiting to complete the
        # order, so it is told on its own page — this is the end of the cycle.
        if sales_order is not None:
            _notify(
                'sales',
                f'Order #{sales_order.id} is ready to fulfil — {product} passed quality inspection (Production #{order.id}).',
                sales_order.id, 'SalesOrder', company, 'sales',
            )
        else:
            _notify(
                'sales',
                f'{product} from Production #{order.id} passed quality and is available to sell.',
                order.id, 'ProductionOrder', company, 'sales',
            )
        _notify('store', f'Quality APPROVED for Production #{order.id} — batch ready for release.', order.id, 'ProductionOrder', company, 'inventory')
        _notify('production', f'Quality APPROVED for Production #{order.id} — batch released.', order.id, 'ProductionOrder', company, 'production')
        _notify('admin', f'Quality check APPROVED for Production #{order.id}.', order.id, 'ProductionOrder', company, 'quality')
    elif instance.status == 'rejected':
        # Step 6: quality failed, so the batch goes back to production for
        # rework. Sales is told only if a customer order is waiting on it.
        remarks = f' Remarks: {instance.remarks}' if getattr(instance, 'remarks', '') else ''
        _notify('production', f'Quality REJECTED batch for Production #{order.id} — rework required.{remarks}', order.id, 'ProductionOrder', company, 'production')
        if sales_order is not None:
            _notify(
                'sales',
                f'Order #{sales_order.id} delayed — {product} failed quality inspection and is being reworked.',
                sales_order.id, 'SalesOrder', company, 'sales',
            )
        _notify('admin', f'Quality check REJECTED for Production #{order.id}.', order.id, 'ProductionOrder', company, 'quality')


# ── Procurement ───────────────────────────────────────────────────────────────

PROCUREMENT_ROLES = ['store', 'finance', 'admin']


@receiver(post_save, sender=PurchaseOrder)
def on_purchase_order_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['vendor', 'status', 'total_amount', 'priority', 'expected_delivery', 'created_at'])
    _push('PurchaseOrder', instance, 'created' if created else 'updated', payload, PROCUREMENT_ROLES)

    company = _safe(instance, 'vendor', 'company')

    if created:
        _notify('admin', f'New Purchase Order #{instance.id} from vendor {instance.vendor.name}.', instance.id, 'PurchaseOrder', company, 'procurement')
        _notify('finance', f'New Purchase Order #{instance.id} worth ${instance.total_amount} requires budget review.', instance.id, 'PurchaseOrder', company, 'finance')
    elif instance.status == 'approved':
        _notify('store', f'Purchase Order #{instance.id} APPROVED — proceed to place order with {instance.vendor.name}.', instance.id, 'PurchaseOrder', company, 'procurement')
    elif instance.status == 'received':
        _notify('store', f'Purchase Order #{instance.id} marked RECEIVED — verify goods receipt.', instance.id, 'PurchaseOrder', company, 'procurement')
        _notify('finance', f'Purchase Order #{instance.id} received — ready for payment processing.', instance.id, 'PurchaseOrder', company, 'finance')


@receiver(post_save, sender=GoodsReceipt)
def on_goods_receipt_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['purchase_order', 'warehouse', 'received_at'])
    _push('GoodsReceipt', instance, 'created' if created else 'updated', payload, PROCUREMENT_ROLES)

    company = _safe(instance, 'purchase_order', 'vendor', 'company')

    if created:
        _notify('finance', f'Goods received for PO #{instance.purchase_order.id} at {instance.warehouse.name} — update accounts payable.', instance.purchase_order.id, 'GoodsReceipt', company, 'finance')
        _notify('store', f'Goods received for PO #{instance.purchase_order.id} at {instance.warehouse.name} — stock updated.', instance.purchase_order.id, 'GoodsReceipt', company, 'inventory')
        _notify('admin', f'Goods receipt recorded for PO #{instance.purchase_order.id}.', instance.purchase_order.id, 'GoodsReceipt', company, 'procurement')


# ── Sales ─────────────────────────────────────────────────────────────────────

SALES_ROLES = ['sales', 'store', 'production', 'admin']


@receiver(post_save, sender=SalesOrder)
def on_sales_order_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['customer', 'status', 'total_amount', 'created_at'])
    _push('SalesOrder', instance, 'created' if created else 'updated', payload, SALES_ROLES)

    company = _company_of_sales_order(instance)

    if created:
        # Steps 1–2: the order arrives and the downstream functions are alerted.
        # Production is told on its page, store on Inventory's — each sees the
        # request where they would act on it.
        _notify('production', f'New Sales Order #{instance.id} from {instance.customer.name} — check if production is needed.', instance.id, 'SalesOrder', company, 'production')
        _notify('store', f'New Sales Order #{instance.id} created — verify stock availability.', instance.id, 'SalesOrder', company, 'inventory')
        _notify('admin', f'New Sales Order #{instance.id} for customer {instance.customer.name} worth ${instance.total_amount}.', instance.id, 'SalesOrder', company, 'sales')
    elif instance.status == 'shipped':
        _notify('finance', f'Sales Order #{instance.id} shipped — raise invoice for ${instance.total_amount}.', instance.id, 'SalesOrder', company, 'finance')
        _notify('sales', f'Sales Order #{instance.id} shipped to {instance.customer.name}.', instance.id, 'SalesOrder', company, 'sales')


# ── Finance ───────────────────────────────────────────────────────────────────

FINANCE_ROLES = ['finance', 'admin']


@receiver(post_save, sender=ExpenseRequest)
def on_expense_request_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['title', 'amount', 'category', 'status', 'created_at'])
    _push('ExpenseRequest', instance, 'created' if created else 'updated', payload, FINANCE_ROLES)

    company = _safe(instance, 'company')

    if created and instance.status == 'pending':
        _notify('finance', f"Expense request '{instance.title}' for ${instance.amount} awaiting approval.", instance.id, 'ExpenseRequest', company, 'finance')
        _notify('admin', f"New expense request: '{instance.title}' — ${instance.amount} ({instance.get_category_display()}).", instance.id, 'ExpenseRequest', company, 'finance')
    elif not created and instance.status in ('approved', 'rejected'):
        role = instance.requested_by.role if instance.requested_by else 'admin'
        word = 'APPROVED' if instance.status == 'approved' else 'REJECTED'
        _notify(role, f"Your expense request '{instance.title}' has been {word}.", instance.id, 'ExpenseRequest', company, 'finance')


# ── Maintenance ───────────────────────────────────────────────────────────────

MAINTENANCE_ROLES = ['production', 'quality', 'admin']


@receiver(post_save, sender=Equipment)
def on_equipment_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['name', 'status', 'health', 'uptime'])
    _push('Equipment', instance, 'created' if created else 'updated', payload, MAINTENANCE_ROLES)


@receiver(post_save, sender=MaintenanceTask)
def on_maintenance_task_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['equipment', 'task_type', 'status', 'priority', 'scheduled_date', 'is_approved'])
    _push('MaintenanceTask', instance, 'created' if created else 'updated', payload, MAINTENANCE_ROLES)

    # Equipment carries no company FK, so maintenance notifications stay
    # company=None and are visible only to the legacy/global scope. Adding that
    # FK is a separate migration — see the note in the handover summary.
    if created:
        _notify('admin', f'New maintenance task requested for {instance.equipment.name} ({instance.task_type}, {instance.priority} priority).', instance.id, 'MaintenanceTask', None, 'maintenance')
    elif not created and instance.status == 'completed':
        _notify('admin', f'Maintenance task for {instance.equipment.name} marked COMPLETED.', instance.id, 'MaintenanceTask', None, 'maintenance')
        _notify('quality', f'Maintenance completed on {instance.equipment.name} — may require post-maintenance quality check.', instance.id, 'MaintenanceTask', None, 'maintenance')
