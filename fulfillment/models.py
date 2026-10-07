"""Finished-goods fulfilment: allocation → picking → packing → staging →
shipment preparation → verification → dispatch (TB-06, TB-10, TB-11).

Every quantity is tracked independently so partial allocation, partial
picking and partial dispatch stay consistent:

    order line quantity  >=  allocated  >=  picked  >=  dispatched

FG allocated to one customer order is not available to any other order.
"""
from django.db import models
from django.utils import timezone


class FGAllocation(models.Model):
    """QA-approved finished goods reserved for one customer order line."""

    STATUS_CHOICES = [
        ("active", "Allocated"),
        ("dispatched", "Dispatched"),
        ("released", "Released"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    sales_order = models.ForeignKey("sales.SalesOrder", on_delete=models.CASCADE, related_name="fg_allocations")
    sales_order_item = models.ForeignKey("sales.SalesOrderItem", on_delete=models.CASCADE, related_name="fg_allocations")
    item = models.ForeignKey("inventory.Item", on_delete=models.CASCADE, related_name="+")
    quantity = models.FloatField()
    # Quantity of this allocation already dispatched; the rest is still held.
    dispatched_quantity = models.FloatField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    @property
    def open_quantity(self):
        if self.status != "active":
            return 0
        return max(self.quantity - self.dispatched_quantity, 0)

    def __str__(self):
        return f"ALLOC-{self.id}: {self.quantity} {self.item} → SO-{self.sales_order_id}"


class Dispatch(models.Model):
    """One delivery to a customer, from pick list to dispatch confirmation."""

    STATUS_CHOICES = [
        ("picking", "Picking"),
        ("picked", "Picked"),
        ("packed", "Packed"),
        ("staged", "Staged"),
        ("prepared", "Shipment Prepared"),
        ("verified", "Dispatch Verified"),
        ("dispatched", "Dispatched"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]
    # Order in which a dispatch moves; used to validate stage transitions.
    FLOW = ["picking", "picked", "packed", "staged", "prepared", "verified", "dispatched", "delivered"]

    MODE_CHOICES = [
        ("road", "Road"),
        ("sea", "Sea"),
        ("air", "Air"),
        ("rail", "Rail"),
        ("courier", "Courier"),
        ("customer_pickup", "Customer Pickup"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    dispatch_number = models.CharField(max_length=30, blank=True, default="", db_index=True)
    sales_order = models.ForeignKey("sales.SalesOrder", on_delete=models.CASCADE, related_name="dispatches")
    warehouse = models.ForeignKey("inventory.Warehouse", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="picking")

    # Logistics (TB-10).
    shipment_mode = models.CharField(max_length=20, choices=MODE_CHOICES, blank=True, default="")
    carrier = models.CharField(max_length=200, blank=True, default="")
    shipment_reference = models.CharField(max_length=100, blank=True, default="", help_text="AWB / B/L / LR / tracking no.")
    vehicle_number = models.CharField(max_length=50, blank=True, default="")
    driver_name = models.CharField(max_length=100, blank=True, default="")
    transport_details = models.TextField(blank=True, default="")
    freight_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    packages = models.PositiveIntegerField(default=0)
    gross_weight = models.CharField(max_length=50, blank=True, default="")

    # Delivery information.
    ship_to_name = models.CharField(max_length=200, blank=True, default="")
    ship_to_address = models.TextField(blank=True, default="")
    expected_delivery = models.DateField(null=True, blank=True)
    received_by = models.CharField(max_length=150, blank=True, default="")
    delivery_notes = models.TextField(blank=True, default="")

    # Stage timestamps and actors.
    picked_at = models.DateTimeField(null=True, blank=True)
    packed_at = models.DateTimeField(null=True, blank=True)
    staged_at = models.DateTimeField(null=True, blank=True)
    prepared_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    # Downstream links. The sales.Shipment carries the lot genealogy
    # (ShipmentLot) and keeps the existing logistics screens working.
    shipment = models.ForeignKey("sales.Shipment", null=True, blank=True, on_delete=models.SET_NULL, related_name="dispatches")
    invoice = models.ForeignKey("sales.Invoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="dispatches")
    invoice_error = models.TextField(blank=True, default="")

    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def total_quantity(self):
        return sum(line.quantity for line in self.lines.all())

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.dispatch_number:
            self.dispatch_number = f"DSP-{self.id:05d}"
            Dispatch.objects.filter(pk=self.pk).update(dispatch_number=self.dispatch_number)

    def __str__(self):
        return self.dispatch_number or f"Dispatch #{self.id}"


class DispatchLine(models.Model):
    """One order line on a dispatch. `quantity` is the picked quantity that
    leaves on this dispatch; it is drawn from the line's allocations."""

    dispatch = models.ForeignKey(Dispatch, on_delete=models.CASCADE, related_name="lines")
    sales_order_item = models.ForeignKey("sales.SalesOrderItem", on_delete=models.CASCADE, related_name="dispatch_lines")
    item = models.ForeignKey("inventory.Item", on_delete=models.CASCADE, related_name="+")
    requested_quantity = models.FloatField()
    quantity = models.FloatField(default=0, help_text="Picked quantity")
    packed = models.BooleanField(default=False)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.quantity} {self.item} on {self.dispatch}"
