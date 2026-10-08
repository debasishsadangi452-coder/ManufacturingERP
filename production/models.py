import datetime

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
    # Line that orders for this product go to automatically (e.g. batches
    # planned from a sales order). Optional; skipped while under maintenance.
    default_line = models.ForeignKey(
        "production.ProductionLine", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
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
        from inventory.models import BOM
        bom = BOM.objects.filter(finished_good=self.product, is_active=True).first()
        if bom:
            return [
                (line, float(line.quantity_in_base_unit()) * units)
                for line in bom.lines.select_related("raw_material").all()
            ]

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

    # One lifecycle used everywhere (Track B, TB-01). "scheduled" is Ready for
    # Production and "running" is In Production; both keep their historic codes
    # so existing screens and reports continue to work. QA progress is tracked
    # separately on `qa_status`, so an order can be Completed and QA Pending.
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("pending_approval", "Pending Approval"),
        ("approved", "Approved"),
        ("material_pending", "Material Pending"),
        ("scheduled", "Ready for Production"),
        ("running", "In Production"),
        ("partially_completed", "Partially Completed"),
        ("completed", "Completed"),
        ("delayed", "Delayed"),
        ("closed", "Closed"),
        ("cancelled", "Cancelled"),
    ]
    QA_STATUS_CHOICES = [
        ("not_started", "Not Started"),
        ("pending", "QA Pending"),
        ("partial", "QA Partially Passed"),
        ("passed", "QA Passed"),
        ("failed", "QA Failed"),
    ]
    # Statuses in which shop-floor work can still be reported.
    ACTIVE_STATUSES = ("approved", "material_pending", "scheduled", "running", "partially_completed", "delayed")
    # Statuses that may be started on the shop floor.
    STARTABLE_STATUSES = ("approved", "material_pending", "scheduled", "delayed")

    order_number = models.CharField(max_length=30, blank=True, default="", db_index=True)
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

    # --- Track B: manufacturing execution -----------------------------------
    # start_time / end_time above are the actual start and completion.
    # Production Plan reference from Track A. Kept as a reference string until
    # the Production Plan model lands, so either track can ship independently.
    production_plan_ref = models.CharField(max_length=50, blank=True, default="", db_index=True)
    produced_quantity = models.FloatField(default=0)
    qa_status = models.CharField(max_length=20, choices=QA_STATUS_CHOICES, default="not_started")
    planned_start = models.DateTimeField(null=True, blank=True)
    planned_end = models.DateTimeField(null=True, blank=True)
    # Rework orders reprocess QA-failed output of another order (TB-18).
    rework_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="rework_orders"
    )
    rework_source_lot = models.ForeignKey(
        "inventory.Batch", null=True, blank=True, on_delete=models.SET_NULL, related_name="rework_orders"
    )
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)

    @property
    def remaining_quantity(self):
        return max((self.quantity or 0) - (self.produced_quantity or 0), 0)

    @property
    def is_rework(self):
        return self.rework_of_id is not None

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.order_number:
            self.order_number = f"PRD-{self.id:05d}"
            ProductionOrder.objects.filter(pk=self.pk).update(order_number=self.order_number)

    def __str__(self):
        return self.order_number or f"Production #{self.id}"


class ProductionMaterialRequirement(models.Model):
    """Material ledger for a production order.

    Required, consumed, and shortage are stored independently so material
    reserved before production is not deducted again when the order completes.
    """

    production_order = models.ForeignKey(
        ProductionOrder, on_delete=models.CASCADE, related_name="material_requirements"
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    required_quantity = models.FloatField(default=0)
    reserved_quantity = models.FloatField(default=0)
    issued_quantity = models.FloatField(default=0)
    consumed_quantity = models.FloatField(default=0)
    shortage_quantity = models.FloatField(default=0)

    class Meta:
        unique_together = ("production_order", "item")

    @property
    def remaining_quantity(self):
        return max(self.required_quantity - self.consumed_quantity, 0)

    def refresh_shortage(self, available_quantity):
        self.shortage_quantity = max(self.required_quantity - self.consumed_quantity - available_quantity, 0)
        return self.shortage_quantity


# ---------------------------------------------------------------------------
# Track B — manufacturing settings, resources, routing, operations, output
# ---------------------------------------------------------------------------

class ManufacturingSettings(models.Model):
    """Per-company switches and formula parameters for Track B.

    The costing and capacity formulas read their parameters from here rather
    than hard-coding them (TB-19), so the client's agreed values are entered
    once and used everywhere.
    """

    OVERHEAD_METHOD_CHOICES = [
        ("none", "No overhead"),
        ("per_unit", "Fixed amount per good unit"),
        ("percent_of_material", "Percent of raw material cost"),
        ("per_machine_hour", "Rate per machine hour"),
        ("per_labour_hour", "Rate per labour hour"),
    ]
    BUSINESS_MODEL_CHOICES = [
        ("mto", "Make to Order"),
        ("mts", "Make to Stock"),
        ("hybrid", "Make to Order and Make to Stock"),
    ]

    company = models.OneToOneField(
        "accounts.Company", on_delete=models.CASCADE, related_name="manufacturing_settings"
    )
    business_model = models.CharField(max_length=10, choices=BUSINESS_MODEL_CHOICES, default="mto")
    customer_order_approval_required = models.BooleanField(default=False)
    production_plan_approval_required = models.BooleanField(default=False)
    require_materials_available_for_conversion = models.BooleanField(default=False)
    include_open_purchase_orders_in_mrp = models.BooleanField(default=True)
    protect_safety_stock_in_mrp = models.BooleanField(default=True)
    allow_partial_production_and_dispatch = models.BooleanField(default=True)
    # Operations must be completed in sequence unless a step allows otherwise.
    enforce_operation_sequence = models.BooleanField(default=True)
    # Incoming raw material is held for inspection before it becomes usable
    # stock. Off by default until the client confirms the RM QC trigger point.
    incoming_qc_required = models.BooleanField(default=False)
    business_rules_confirmed = models.BooleanField(default=False)
    business_rules_notes = models.TextField(blank=True, default="")
    # Create the invoice automatically when a dispatch is confirmed.
    auto_invoice_on_dispatch = models.BooleanField(default=True)
    overhead_method = models.CharField(max_length=30, choices=OVERHEAD_METHOD_CHOICES, default="none")
    overhead_rate = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    # Capacity defaults for resources that do not set their own calendar.
    default_hours_per_shift = models.FloatField(default=8)
    # First shift start; the scheduler places work from here each working day.
    shift_start_time = models.TimeField(default=datetime.time(8, 0))
    default_shifts_per_day = models.FloatField(default=1)
    default_working_days_per_week = models.FloatField(default=6)
    # Utilisation above this percentage is flagged as overloaded.
    overload_threshold_percent = models.FloatField(default=100)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def for_company(cls, company):
        if company is None:
            return cls()
        obj, _ = cls.objects.get_or_create(company=company)
        return obj

    def __str__(self):
        return f"Manufacturing settings - {self.company}"


class Resource(models.Model):
    """A machine or manpower resource that operations are assigned to (TB-07)."""

    TYPE_CHOICES = [("machine", "Machine"), ("manpower", "Manpower")]
    STATUS_CHOICES = [
        ("available", "Available"),
        ("busy", "Busy"),
        ("maintenance", "Maintenance"),
        ("unavailable", "Unavailable"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    code = models.CharField(max_length=30, blank=True, default="")
    name = models.CharField(max_length=200)
    resource_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    category = models.CharField(max_length=100, blank=True, default="", help_text="Machine type or labour role")
    skill = models.CharField(max_length=100, blank=True, default="")
    line = models.ForeignKey(ProductionLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="resources")
    equipment = models.ForeignKey(
        "maintenance.Equipment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    employee = models.ForeignKey(
        "workforce.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    location = models.CharField(max_length=200, blank=True, default="")
    shift_name = models.CharField(max_length=100, blank=True, default="")
    # Calendar. Blank values fall back to ManufacturingSettings defaults.
    hours_per_shift = models.FloatField(null=True, blank=True)
    shifts_per_day = models.FloatField(null=True, blank=True)
    working_days_per_week = models.FloatField(null=True, blank=True)
    # Standard output rate for machines, in units per hour.
    run_rate_per_hour = models.FloatField(default=0)
    # Number of identical machines or people this resource represents.
    units = models.PositiveIntegerField(default=1)
    cost_per_hour = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="available")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["resource_type", "name"]

    def calendar(self, settings=None):
        settings = settings or ManufacturingSettings.for_company(self.company)
        return {
            "hours_per_shift": self.hours_per_shift if self.hours_per_shift is not None else settings.default_hours_per_shift,
            "shifts_per_day": self.shifts_per_day if self.shifts_per_day is not None else settings.default_shifts_per_day,
            "working_days_per_week": (
                self.working_days_per_week if self.working_days_per_week is not None
                else settings.default_working_days_per_week
            ),
        }

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.code:
            self.code = f"{'MC' if self.resource_type == 'machine' else 'MP'}-{self.id:04d}"
            Resource.objects.filter(pk=self.pk).update(code=self.code)

    def __str__(self):
        return f"{self.code} {self.name}"


class ResourceUnavailability(models.Model):
    """A period in which a resource cannot work (maintenance, leave, breakdown)."""

    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="unavailability")
    start = models.DateTimeField()
    end = models.DateTimeField()
    reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["start"]


class RoutingStep(models.Model):
    """One step of a product's routing; copied onto each production order of
    that recipe as a ProductionOperation (TB-03)."""

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="routing_steps")
    sequence = models.PositiveIntegerField()
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    machine = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    manpower = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    setup_minutes = models.FloatField(default=0)
    run_minutes_per_unit = models.FloatField(default=0)
    # Direct operation cost not driven by resource time (consumables, outside processing).
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    # When true, this step may be completed before the previous one.
    allow_out_of_sequence = models.BooleanField(default=False)

    class Meta:
        ordering = ["recipe", "sequence"]
        unique_together = ("recipe", "sequence")

    def __str__(self):
        return f"{self.recipe.product.name} · Op {self.sequence} {self.name}"


class ProductionOperation(models.Model):
    """An operation of one production order on the shop floor."""

    STATUS_CHOICES = [
        ("not_started", "Not Started"),
        ("ready", "Ready"),
        ("in_progress", "In Progress"),
        ("paused", "Paused"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("rework_required", "Rework Required"),
    ]

    production_order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name="operations")
    routing_step = models.ForeignKey(RoutingStep, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sequence = models.PositiveIntegerField()
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    planned_quantity = models.FloatField(default=0)
    started_quantity = models.FloatField(default=0)
    completed_quantity = models.FloatField(default=0)
    rejected_quantity = models.FloatField(default=0)
    rework_quantity = models.FloatField(default=0)
    scrap_quantity = models.FloatField(default=0)
    planned_start = models.DateTimeField(null=True, blank=True)
    planned_end = models.DateTimeField(null=True, blank=True)
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)
    machine = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="machine_operations")
    manpower = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="manpower_operations")
    operator = models.CharField(max_length=150, blank=True, default="")
    setup_minutes = models.FloatField(default=0)
    run_minutes_per_unit = models.FloatField(default=0)
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    # Recorded working time; falls back to actual_start/actual_end when blank.
    actual_minutes = models.FloatField(null=True, blank=True)
    allow_out_of_sequence = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="not_started")
    notes = models.TextField(blank=True, default="")
    # True for the single step created when a recipe has no routing yet.
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["production_order", "sequence"]
        unique_together = ("production_order", "sequence")

    @property
    def planned_minutes(self):
        return (self.setup_minutes or 0) + (self.run_minutes_per_unit or 0) * (self.planned_quantity or 0)

    @property
    def remaining_minutes(self):
        if self.status == "completed":
            return 0
        left = max((self.planned_quantity or 0) - (self.completed_quantity or 0), 0)
        setup = 0 if self.actual_start else (self.setup_minutes or 0)
        return setup + (self.run_minutes_per_unit or 0) * left

    def worked_minutes(self):
        if self.actual_minutes is not None:
            return self.actual_minutes
        if self.actual_start:
            end = self.actual_end or timezone.now()
            return max((end - self.actual_start).total_seconds() / 60, 0)
        return 0

    def __str__(self):
        return f"{self.production_order} · Op {self.sequence} {self.name}"


class ProductionOutput(models.Model):
    """One reported quantity of finished output (partial production, TB-01).

    Each output gets its own lot and QA check, so partial production and
    partial QA are tracked independently.
    """

    production_order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name="outputs")
    quantity = models.FloatField()
    lot = models.ForeignKey("inventory.Batch", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reported_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.quantity} of {self.production_order}"


class ScrapRecord(models.Model):
    """Scrapped material or product with its cost impact (TB-18)."""

    SOURCE_CHOICES = [
        ("operation", "Shop floor operation"),
        ("qa", "QA rejection"),
        ("manual", "Manual entry"),
    ]

    production_order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name="scrap_records")
    operation = models.ForeignKey(ProductionOperation, null=True, blank=True, on_delete=models.SET_NULL, related_name="scrap_records")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    lot = models.ForeignKey("inventory.Batch", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    quantity = models.FloatField()
    reason = models.CharField(max_length=255, blank=True, default="")
    resource = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="operation")
    # How the scrap reaches the GL (TB-14). Scrap on an original production
    # order is already inside its manufacturing entry ("covered"); the rest is
    # written off by its own entry.
    GL_TREATMENT_CHOICES = [
        ("covered", "Included in the production order's manufacturing entry"),
        ("finished_goods", "Write off from finished goods inventory"),
        ("raw_material", "Write off from raw material inventory"),
    ]
    gl_treatment = models.CharField(max_length=20, choices=GL_TREATMENT_CHOICES, default="covered")
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    cost_impact = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    recorded_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
class ProductionPlan(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("pending_approval", "Pending Approval"),
        ("approved", "Approved"),
        ("planned", "Planned"),
        ("partially_converted", "Partially Converted"),
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
        return self.status == "converted"

    @property
    def remaining_quantity(self):
        from django.db.models import Sum
        scheduled = self.production_orders.exclude(status="cancelled").aggregate(total=Sum("quantity"))["total"] or 0
        return max(float(self.planned_quantity) - float(scheduled), 0)

    def convert_to_production_order(self, warehouse=None, line=None, start_time=None, end_time=None, user=None,
                                    quantity=None):
        """
        Converts this Production Plan into a Production Order atomically.
        Validates that it has not already been converted and that a recipe exists.
        """
        from django.db import transaction
        from django.core.exceptions import ValidationError

        with transaction.atomic():
            locked_plan = ProductionPlan.objects.select_for_update().get(pk=self.pk)
            if locked_plan.status == "cancelled":
                raise ValidationError("Cannot convert a cancelled Production Plan.")
            if locked_plan.status == "converted":
                raise ValidationError("This Production Plan has already been converted to a Production Order.")

            settings = ManufacturingSettings.for_company(locked_plan.company)
            if (
                settings.business_model == "mto"
                and locked_plan.sales_order
                and locked_plan.sales_order.status != "confirmed"
            ):
                raise ValidationError("Confirm the customer order before converting its production plan.")
            if settings.production_plan_approval_required and locked_plan.status != "approved":
                raise ValidationError("Production Plan must be approved before conversion.")

            recipe = Recipe.objects.filter(product=locked_plan.item).first()
            if not recipe:
                raise ValidationError(f"No recipe found for product '{locked_plan.item.name}'. Cannot convert to Production Order.")

            if not warehouse:
                if locked_plan.company:
                    warehouse = Warehouse.objects.filter(
                        company=locked_plan.company, warehouse_type="production"
                    ).first()
                default_warehouse = locked_plan.item.default_warehouse
                if (
                    not warehouse
                    and default_warehouse
                    and default_warehouse.company_id in (None, locked_plan.company_id)
                ):
                    warehouse = default_warehouse
                if not warehouse:
                    warehouse = Warehouse.objects.filter(company=locked_plan.company).first()
                if not warehouse:
                    warehouse = Warehouse.objects.first()
                if not warehouse:
                    raise ValidationError("No warehouse available for production.")

            remaining = locked_plan.remaining_quantity
            conversion_quantity = float(quantity if quantity is not None else remaining)
            if conversion_quantity <= 0 or conversion_quantity > remaining:
                raise ValidationError(
                    f"Conversion quantity must be greater than zero and no more than {remaining:g}."
                )
            if not settings.allow_partial_production_and_dispatch and conversion_quantity < remaining - 1e-6:
                raise ValidationError("Partial production is disabled in manufacturing settings.")
            if settings.require_materials_available_for_conversion:
                from .planning import material_check
                material_status = material_check(recipe, conversion_quantity)
                if not material_status["can_produce"]:
                    raise ValidationError({"materials": material_status["warnings"]})

            from .execution import create_production_order, reserve_materials
            prod_order, _checks = create_production_order(
                recipe, conversion_quantity, warehouse,
                sales_order=locked_plan.sales_order,
                plan_ref=locked_plan.plan_number,
                line=line,
                planned_start=start_time,
                planned_end=end_time,
                plan_approved=(
                    locked_plan.status == "approved"
                    or not settings.production_plan_approval_required
                ),
            )
            prod_order.production_plan = locked_plan
            prod_order.save(update_fields=["production_plan"])
            shortages = reserve_materials(prod_order, user)
            if shortages:
                prod_order.status = "material_pending"
                prod_order.save(update_fields=["status"])

            from .scheduling import schedule_order
            schedule_order(prod_order, start=start_time)

            locked_plan.status = "converted" if locked_plan.remaining_quantity <= 1e-6 else "partially_converted"
            locked_plan.save(update_fields=["status", "updated_at"])
            self.status = locked_plan.status
            return prod_order

    def __str__(self):
        return f"{self.plan_number} - {self.item.name} ({self.planned_quantity})"
