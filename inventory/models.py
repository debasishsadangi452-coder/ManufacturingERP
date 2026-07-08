from django.db import models

class Item(models.Model):
    CATEGORY_CHOICES = [
        ("raw_material", "Raw Material"),
        ("finished_good", "Finished Good"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    unit = models.CharField(max_length=50, default="unit")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uniq_item_name_per_company"),
        ]

    @property
    def is_finished_good(self):
        return self.category == "finished_good"

    def __str__(self):
        return self.name


class Warehouse(models.Model):
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200)


class Stock(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    quantity = models.FloatField(default=0)

    class Meta:
        unique_together = ("item", "warehouse")



class Batch(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    batch_number = models.CharField(max_length=100)
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.FloatField()

class StockMovement(models.Model):

    MOVEMENT_TYPES = [
        ("IN", "Stock In"),
        ("OUT", "Stock Out"),
        ("ADJUST", "Adjustment"),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)

    movement_type = models.CharField(max_length=10, choices=MOVEMENT_TYPES)

    quantity = models.FloatField()
    reference = models.CharField(max_length=200, blank=True)

    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.item} {self.movement_type} {self.quantity}"


class InventoryRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("procuring", "Procuring"),
        ("supplied", "Supplied"),
        ("cancelled", "Cancelled"),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    quantity = models.FloatField()
    production_order = models.ForeignKey("production.ProductionOrder", on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Request: {self.item.name} ({self.quantity}) for {self.production_order}"
