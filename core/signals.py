from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from production.models import ProductionOrder, RecipeIngredient
from quality.models import QualityCheck
from inventory.models import Stock, InventoryRequest, Item
from sales.models import SalesOrder
from procurement.models import PurchaseOrder, GoodsReceipt
from finance.models import ExpenseRequest
from maintenance.models import MaintenanceTask, Equipment
from core.models import Notification, DataChangeEvent


# ── Helpers ───────────────────────────────────────────────────────────────────

def _notify(role, message, related_id=None, related_type=""):
    Notification.objects.create(
        recipient_role=role,
        message=message,
        related_id=related_id,
        related_type=related_type,
    )


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

    if instance.quantity < 50 and instance.item.category == 'raw_material':
        if not Notification.objects.filter(
            message__contains=f'Low stock: {instance.item.name}', is_read=False
        ).exists():
            _notify('store', f'Low stock: {instance.item.name} ({instance.quantity} {instance.item.unit} left). Consider procurement.', instance.item.id, 'Item')
            _notify('admin', f'Low stock alert: {instance.item.name} has only {instance.quantity} {instance.item.unit} remaining.', instance.item.id, 'Item')


@receiver(post_save, sender=InventoryRequest)
def on_inventory_request_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['item', 'warehouse', 'quantity', 'status', 'created_at'])
    _push('InventoryRequest', instance, 'created' if created else 'updated', payload, INVENTORY_ROLES)

    if created:
        _notify('store', f'New inventory request: {instance.quantity} {instance.item.name} needed at {instance.warehouse.name}.', instance.id, 'InventoryRequest')
        _notify('admin', f'Inventory request #{instance.id}: {instance.quantity} {instance.item.name} — status: {instance.status}.', instance.id, 'InventoryRequest')
    elif instance.status == 'supplied':
        _notify('production', f'Inventory request #{instance.id} fulfilled — {instance.quantity} {instance.item.name} delivered to {instance.warehouse.name}.', instance.id, 'InventoryRequest')


# ── Production ────────────────────────────────────────────────────────────────

PRODUCTION_ROLES = ['production', 'admin']


@receiver(post_save, sender=ProductionOrder)
def on_production_order_change(sender, instance, created, **kwargs):
    try:
        payload = _serialize(instance, ['recipe', 'warehouse', 'quantity', 'status'])
        _push('ProductionOrder', instance, 'created' if created else 'updated', payload, PRODUCTION_ROLES)

        if created:
            _notify('admin', f'New Production Order #{instance.id} created for {instance.quantity} units.', instance.id, 'ProductionOrder')

            ingredients = RecipeIngredient.objects.filter(recipe=instance.recipe)
            shortages = []
            for ing in ingredients:
                required = ing.quantity * instance.quantity
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
                _notify('store', f'Material shortage for Production #{instance.id}: {", ".join(shortages)}. Inventory request created.', instance.id, 'ProductionOrder')

        if not created and instance.status == 'completed':
            if not QualityCheck.objects.filter(production_order=instance).exists():
                QualityCheck.objects.create(
                    production_order=instance,
                    status='pending',
                    test_type='Post-Production',
                    parameter='Visual & Weight',
                )
                _notify('quality', f'Production Order #{instance.id} completed — batch requires quality inspection.', instance.id, 'ProductionOrder')
                _notify('admin', f'Production Order #{instance.id} completed and sent to quality.', instance.id, 'ProductionOrder')
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f'Signal error on ProductionOrder #{instance.pk}: {e}', exc_info=True)


# ── Quality ───────────────────────────────────────────────────────────────────

QUALITY_ROLES = ['quality', 'production', 'admin']


@receiver(post_save, sender=QualityCheck)
def on_quality_check_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['production_order', 'status', 'test_type', 'parameter', 'result'])
    _push('QualityCheck', instance, 'created' if created else 'updated', payload, QUALITY_ROLES)

    if instance.status == 'approved':
        _notify('store', f'Quality APPROVED for Production #{instance.production_order.id} — batch ready for release.', instance.production_order.id, 'ProductionOrder')
        _notify('admin', f'Quality check APPROVED for Production #{instance.production_order.id}.', instance.production_order.id, 'ProductionOrder')
    elif instance.status == 'rejected':
        _notify('production', f'Quality REJECTED batch for Production #{instance.production_order.id} — rework required.', instance.production_order.id, 'ProductionOrder')
        _notify('admin', f'Quality check REJECTED for Production #{instance.production_order.id}.', instance.production_order.id, 'ProductionOrder')


# ── Procurement ───────────────────────────────────────────────────────────────

PROCUREMENT_ROLES = ['store', 'finance', 'admin']


@receiver(post_save, sender=PurchaseOrder)
def on_purchase_order_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['vendor', 'status', 'total_amount', 'priority', 'expected_delivery', 'created_at'])
    _push('PurchaseOrder', instance, 'created' if created else 'updated', payload, PROCUREMENT_ROLES)

    if created:
        _notify('admin', f'New Purchase Order #{instance.id} from vendor {instance.vendor.name}.', instance.id, 'PurchaseOrder')
        _notify('finance', f'New Purchase Order #{instance.id} worth ${instance.total_amount} requires budget review.', instance.id, 'PurchaseOrder')
    elif instance.status == 'approved':
        _notify('store', f'Purchase Order #{instance.id} APPROVED — proceed to place order with {instance.vendor.name}.', instance.id, 'PurchaseOrder')
    elif instance.status == 'received':
        _notify('store', f'Purchase Order #{instance.id} marked RECEIVED — verify goods receipt.', instance.id, 'PurchaseOrder')
        _notify('finance', f'Purchase Order #{instance.id} received — ready for payment processing.', instance.id, 'PurchaseOrder')


@receiver(post_save, sender=GoodsReceipt)
def on_goods_receipt_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['purchase_order', 'warehouse', 'received_at'])
    _push('GoodsReceipt', instance, 'created' if created else 'updated', payload, PROCUREMENT_ROLES)

    if created:
        _notify('finance', f'Goods received for PO #{instance.purchase_order.id} at {instance.warehouse.name} — update accounts payable.', instance.purchase_order.id, 'GoodsReceipt')
        _notify('admin', f'Goods receipt recorded for PO #{instance.purchase_order.id}.', instance.purchase_order.id, 'GoodsReceipt')


# ── Sales ─────────────────────────────────────────────────────────────────────

SALES_ROLES = ['sales', 'store', 'production', 'admin']


@receiver(post_save, sender=SalesOrder)
def on_sales_order_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['customer', 'status', 'total_amount', 'created_at'])
    _push('SalesOrder', instance, 'created' if created else 'updated', payload, SALES_ROLES)

    if created:
        _notify('production', f'New Sales Order #{instance.id} from {instance.customer.name} — check if production is needed.', instance.id, 'SalesOrder')
        _notify('store', f'New Sales Order #{instance.id} created — verify stock availability.', instance.id, 'SalesOrder')
        _notify('admin', f'New Sales Order #{instance.id} for customer {instance.customer.name} worth ${instance.total_amount}.', instance.id, 'SalesOrder')
    elif instance.status == 'shipped':
        _notify('finance', f'Sales Order #{instance.id} shipped — raise invoice for ${instance.total_amount}.', instance.id, 'SalesOrder')


# ── Finance ───────────────────────────────────────────────────────────────────

FINANCE_ROLES = ['finance', 'admin']


@receiver(post_save, sender=ExpenseRequest)
def on_expense_request_change(sender, instance, created, **kwargs):
    payload = _serialize(instance, ['title', 'amount', 'category', 'status', 'created_at'])
    _push('ExpenseRequest', instance, 'created' if created else 'updated', payload, FINANCE_ROLES)

    if created and instance.status == 'pending':
        _notify('finance', f"Expense request '{instance.title}' for ${instance.amount} awaiting approval.", instance.id, 'ExpenseRequest')
        _notify('admin', f"New expense request: '{instance.title}' — ${instance.amount} ({instance.get_category_display()}).", instance.id, 'ExpenseRequest')
    elif not created and instance.status in ('approved', 'rejected'):
        role = instance.requested_by.role if instance.requested_by else 'admin'
        word = 'APPROVED' if instance.status == 'approved' else 'REJECTED'
        _notify(role, f"Your expense request '{instance.title}' has been {word}.", instance.id, 'ExpenseRequest')


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

    if created:
        _notify('admin', f'New maintenance task requested for {instance.equipment.name} ({instance.task_type}, {instance.priority} priority).', instance.id, 'MaintenanceTask')
    elif not created and instance.status == 'completed':
        _notify('admin', f'Maintenance task for {instance.equipment.name} marked COMPLETED.', instance.id, 'MaintenanceTask')
        _notify('quality', f'Maintenance completed on {instance.equipment.name} — may require post-maintenance quality check.', instance.id, 'MaintenanceTask')
