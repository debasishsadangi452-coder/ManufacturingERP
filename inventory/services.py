from django.db import transaction
from .models import Stock, StockMovement


def get_or_create_stock(item, warehouse):
    stock, _ = Stock.objects.get_or_create(
        item=item,
        warehouse=warehouse,
        defaults={"quantity": 0},
    )
    return stock

@transaction.atomic
def increase_stock(item, warehouse, qty, user=None, reference=""):

    stock = get_or_create_stock(item, warehouse)

    stock.quantity += qty
    stock.save()

    StockMovement.objects.create(
        item=item,
        warehouse=warehouse,
        movement_type="IN",
        quantity=qty,
        reference=reference,
        created_by=user,
    )

@transaction.atomic
def decrease_stock(item, warehouse, qty, user=None, reference=""):

    stock = get_or_create_stock(item, warehouse)

    if stock.quantity < qty:
        shortage = qty - stock.quantity
        raise ValueError(f"Insufficient stock for {item.name}: Required {qty}, Available {stock.quantity}, Short {shortage}")

    stock.quantity -= qty
    stock.save()

    StockMovement.objects.create(
        item=item,
        warehouse=warehouse,
        movement_type="OUT",
        quantity=qty,
        reference=reference,
        created_by=user,
    )

@transaction.atomic
def adjust_stock(item, warehouse, new_qty, user=None, reference=""):

    stock = get_or_create_stock(item, warehouse)

    diff = new_qty - stock.quantity

    stock.quantity = new_qty
    stock.save()

    StockMovement.objects.create(
        item=item,
        warehouse=warehouse,
        movement_type="ADJUST",
        quantity=diff,
        reference=reference,
        created_by=user,
    )
