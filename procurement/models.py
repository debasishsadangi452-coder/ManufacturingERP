from django.db import models
from inventory.models import Item, Warehouse
from decimal import Decimal


class Vendor(models.Model):
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, default="raw_material")
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    rating = models.FloatField(default=0.0)

    def __str__(self):
        return self.name


class VendorPriceList(models.Model):
    """
    Price catalog: a vendor's quoted price per unit for each item.
    store_user sets these; used to auto-fill PO line items.
    """
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="price_list")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="vendor_prices")
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    min_order_qty = models.FloatField(default=1, help_text="Minimum order quantity")
    lead_time_days = models.IntegerField(default=7, help_text="Vendor lead time in days")
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    effective_date = models.DateField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("vendor", "item")
        ordering = ["vendor", "item"]

    def __str__(self):
        return f"{self.vendor.name} → {self.item.name} @ {self.currency} {self.unit_price}"


class PurchaseOrder(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    expected_delivery = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=20, default="normal")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("ordered", "Ordered"),
        ("received", "Received"),
        ("cancelled", "Cancelled"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")

    def recalculate_total(self):
        """Recalculate total_amount from line items."""
        total = sum(
            (item.unit_price or Decimal("0")) * Decimal(str(item.quantity))
            for item in self.items.all()
        )
        self.total_amount = total
        self.save(update_fields=["total_amount"])

    def __str__(self):
        return f"PO-{self.id}"


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    quantity = models.FloatField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    @property
    def total_price(self):
        return self.unit_price * Decimal(str(self.quantity))

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Recalculate PO total whenever a line item changes
        self.purchase_order.recalculate_total()

    def delete(self, *args, **kwargs):
        po = self.purchase_order
        super().delete(*args, **kwargs)
        po.recalculate_total()

    def __str__(self):
        return f"{self.quantity} x {self.item.name} @ {self.unit_price}"


class GoodsReceipt(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    received_at = models.DateTimeField(auto_now_add=True)
