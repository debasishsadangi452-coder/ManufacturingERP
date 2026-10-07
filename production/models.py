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
    production_plan = models.ForeignKey(
        "production.ProductionPlan", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="production_orders",
        help_text="The Production Plan Order from which this batch was raised.",
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


class ProductionPlan(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("planned", "Planned"),
        ("converted", "Converted to Production"),
        ("cancelled", "Cancelled"),
    ]

    plan_number = models.CharField(max_length=64, unique=True, db_index=True)
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="production_plans"
    )
    sales_order = models.ForeignKey(
        "sales.SalesOrder", on_delete=models.CASCADE, related_name="production_plans"
    )
    sales_order_item = models.ForeignKey(
        "sales.SalesOrderItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="production_plans"
    )
    customer = models.ForeignKey(
        "sales.Customer", on_delete=models.SET_NULL, null=True, blank=True, related_name="production_plans"
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name="production_plans"
    )
    order_quantity = models.FloatField()
    planned_quantity = models.FloatField()
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default="draft")
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_production_plans"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.sales_order_id:
            if not self.customer_id and hasattr(self.sales_order, 'customer_id') and self.sales_order.customer_id:
                self.customer = self.sales_order.customer
            if not self.company_id and self.customer and getattr(self.customer, 'company_id', None):
                self.company = self.customer.company

        if not self.plan_number:
            import time
            ts = int(time.time() * 1000) % 1000000
            order_id = self.sales_order_id or "0"
            candidate = f"PPO-{order_id}-{ts}"
            while ProductionPlan.objects.filter(plan_number=candidate).exists():
                ts += 1
                candidate = f"PPO-{order_id}-{ts}"
            self.plan_number = candidate
        super().save(*args, **kwargs)

    @property
    def is_converted(self):
        return self.status == "converted" or self.production_orders.exists()

    def convert_to_production_order(self, warehouse=None, line=None, start_time=None, end_time=None, user=None):
        """
        Converts this Production Plan into a Production Order atomically.
        Validates that it has not already been converted and that a recipe exists.
        """
        from django.db import transaction
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            locked_plan = ProductionPlan.objects.select_for_update().get(pk=self.pk)
            if locked_plan.status == "converted" or locked_plan.production_orders.exists():
                raise ValidationError("This Production Plan has already been converted to a Production Order.")

            if locked_plan.status == "cancelled":
                raise ValidationError("Cannot convert a cancelled Production Plan.")

            recipe = Recipe.objects.filter(product=locked_plan.item).first()
            if not recipe:
                raise ValidationError(f"No recipe found for product '{locked_plan.item.name}'. Cannot convert to Production Order.")

            if not warehouse:
                if locked_plan.company:
                    warehouse = Warehouse.objects.filter(company=locked_plan.company).first()
                if not warehouse:
                    warehouse = Warehouse.objects.first()
                if not warehouse:
                    raise ValidationError("No warehouse available for production.")

            prod_order = ProductionOrder.objects.create(
                recipe=recipe,
                quantity=locked_plan.planned_quantity,
                warehouse=warehouse,
                line=line,
                sales_order=locked_plan.sales_order,
                production_plan=locked_plan,
                start_time=start_time,
                end_time=end_time,
                status="scheduled",
            )

            locked_plan.status = "converted"
            locked_plan.save(update_fields=["status", "updated_at"])
            self.status = "converted"
            return prod_order

    def __str__(self):
        return f"{self.plan_number} - {self.item.name} ({self.planned_quantity})"

