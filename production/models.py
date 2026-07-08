from django.db import models
from inventory.models import Item, Warehouse
from django.utils import timezone

class Recipe(models.Model):
    product = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="recipes"
    )

    def __str__(self):
        return f"Recipe for {self.product}"


class RecipeIngredient(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    quantity = models.FloatField()  # per unit of product


class ProductionLine(models.Model):
    STATUS_CHOICES = [
        ("running", "Running"),
        ("maintenance", "Maintenance"),
    ]
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=100)
    location = models.CharField(max_length=200, default="Main Facility")
    capacity = models.FloatField(default=100.0)  # Units per hour standard
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running")

    def __str__(self):
        return f"{self.name} ({self.location})"


class ProductionOrder(models.Model):

    STATUS_CHOICES = [
        ("scheduled", "Scheduled"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("delayed", "Delayed"),
    ]

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    quantity = models.FloatField()
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    line = models.ForeignKey(ProductionLine, on_delete=models.SET_NULL, null=True, blank=True)
    
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="scheduled"
    )
    materials_reserved = models.BooleanField(default=False)  # True if raw materials were pre-deducted at reservation time

    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Production #{self.id}"
