from django.db import models
from inventory.models import Item, Warehouse
from django.utils import timezone

class Recipe(models.Model):
    product = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="recipes"
    )
    # Units of finished good produced by one batch. Drives the cases→batches
    # planner so "200 cases ordered" auto-computes "N batches to make",
    # replacing the second spreadsheet the customer maintains by hand.
    batch_size = models.FloatField(
        default=1, help_text="Finished units produced per production batch"
    )

    def batches_for(self, units):
        """How many whole batches are needed to make `units` of product.

        Rounds up — you can't make a partial batch — and reports the overrun so
        planning is honest about the extra units produced.
        """
        import math
        size = self.batch_size or 1
        batches = math.ceil(units / size) if units > 0 else 0
        produced = batches * size
        return {
            "units_requested": units,
            "batch_size": size,
            "batches": batches,
            "units_produced": produced,
            "overrun": produced - units,
        }

    def material_requirements(self, units):
        """Raw material needed to make `units` of finished product.

        RecipeIngredient.quantity is stated per *batch*, not per unit, so the
        requirement scales by whole batches (you cannot run a partial batch).
        Every planning path goes through here so the availability check, the
        reservation, and the actual lot consumption can never disagree.

        Returns [(ingredient, required_qty), ...].
        """
        batches = self.batches_for(units)["batches"]
        return [
            (ing, ing.quantity * batches)
            for ing in self.recipeingredient_set.select_related("item")
        ]

    def __str__(self):
        return f"Recipe for {self.product}"


class RecipeIngredient(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    # Quantity consumed per BATCH of the recipe (see Recipe.batch_size), not per
    # finished unit. Use Recipe.material_requirements() rather than multiplying
    # this by a unit count.
    quantity = models.FloatField()


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
    # The order Inventory sent to Production, if this batch was raised to fill one.
    # Null for batches started directly from the Production screen (make-to-stock).
    sales_order = models.ForeignKey(
        "sales.SalesOrder", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="production_orders",
    )
    
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
