from django.db import models
from inventory.models import Item, Warehouse


# -------------------------------------------------
# 👤 Customer
# -------------------------------------------------

class Customer(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)

    def __str__(self):
        return self.name


# -------------------------------------------------
# 🧾 Sales Order
# -------------------------------------------------

class SalesOrder(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )

    def __str__(self):
        return f"SO-{self.id}"

class SalesOrderItem(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    quantity = models.FloatField()
    shipped_quantity = models.FloatField(default=0.0)

class Shipment(models.Model):
    STATUS_CHOICES = [
        ("preparing", "Preparing"),
        ("loading", "Loading"),
        ("in-transit", "In Transit"),
        ("delivered", "Delivered"),
        ("delayed", "Delayed"),
    ]
    PRIORITY_CHOICES = [
        ("normal", "Normal"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]

    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    shipped_at = models.DateTimeField(auto_now_add=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="preparing")
    driver = models.CharField(max_length=100, blank=True)
    vehicle = models.CharField(max_length=100, blank=True)
    departure_time = models.CharField(max_length=50, blank=True)
    estimated_arrival = models.CharField(max_length=50, blank=True)
    progress = models.IntegerField(default=0)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default="normal")
    temperature = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"Shipment for SO-{self.sales_order.id}"

