# inventory/services.py

from django.db import transaction
from django.db.models import F
from rest_framework.exceptions import ValidationError
from .models import Stock


# -------------------------------------------------
# 📉 Decrease Stock
# -------------------------------------------------

@transaction.atomic
def decrease_stock(item, warehouse, quantity, user=None, reference=None):

    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    try:
        # Lock the row to prevent race conditions
        stock = (
            Stock.objects
            .select_for_update()
            .get(item=item, warehouse=warehouse)
        )
    except Stock.DoesNotExist:
        raise ValidationError(
            f"No stock record found for '{item.name}' in warehouse '{warehouse.name}'."
        )

    if stock.quantity < quantity:
        raise ValidationError(
            f"Insufficient stock for '{item.name}'. "
            f"Available: {stock.quantity}, Required: {quantity}"
        )

    # Deduct safely
    stock.quantity = F("quantity") - quantity
    stock.save()
    stock.refresh_from_db()

    return stock


# -------------------------------------------------
# 📈 Increase Stock
# -------------------------------------------------

@transaction.atomic
def increase_stock(item, warehouse, quantity, user=None, reference=None):

    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    stock, created = Stock.objects.select_for_update().get_or_create(
        item=item,
        warehouse=warehouse,
        defaults={"quantity": 0}
    )

    stock.quantity = F("quantity") + quantity
    stock.save()
    stock.refresh_from_db()

    return stock