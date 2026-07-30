from django.db import models


class UnitOfMeasure(models.Model):
    """A unit that quantities can be expressed in and converted between.

    Conversion is only valid within a dimension (mass↔mass, count↔count).
    Every unit stores its factor to the dimension's base unit, so converting
    from A to B is: qty * A.to_base_factor / B.to_base_factor. This is what
    lets a raw material bought in pounds, stocked in grams, and consumed in a
    recipe in ounces all reconcile automatically.
    """

    DIMENSION_CHOICES = [
        ("mass", "Mass"),
        ("count", "Count"),
        ("volume", "Volume"),
    ]

    # Company-scoped so tenants can add their own units, but the standard set
    # is seeded company=NULL (shared). Lookups check company OR shared.
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    code = models.CharField(max_length=20, help_text="Short code, e.g. g, oz, lb, case, each")
    name = models.CharField(max_length=50)
    dimension = models.CharField(max_length=20, choices=DIMENSION_CHOICES)
    # Multiplier to the dimension's base unit (base unit itself = 1).
    to_base_factor = models.DecimalField(max_digits=20, decimal_places=8, default=1)
    is_base = models.BooleanField(default=False, help_text="The reference unit for its dimension")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_uom_code_per_company"),
        ]
        ordering = ["dimension", "code"]

    def __str__(self):
        return self.code


class Item(models.Model):
    CATEGORY_CHOICES = [
        ("raw_material", "Raw Material"),
        ("finished_good", "Finished Good"),
    ]

    ERP_CLASSIFICATION_CHOICES = [
        ("raw_material", "Raw Material"),
        ("finished_good", "Finished Good"),
        ("out_of_scope", "Out of Scope"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    unit = models.CharField(max_length=50, default="unit")
    # Real units-of-measure (nullable during backfill; the free-text `unit`
    # above is retained until every item is mapped). base_unit is how stock is
    # held/consumed; purchase_unit is how it's bought.
    base_unit = models.ForeignKey(
        "inventory.UnitOfMeasure", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    purchase_unit = models.ForeignKey(
        "inventory.UnitOfMeasure", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    # Sale price per unit for finished goods; drives sales order totals
    # and the revenue figures on the finance dashboard.
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)
    sku = models.CharField(max_length=100, blank=True)
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reorder_point = models.FloatField(null=True, blank=True)
    # Onboarding fields
    erp_classification = models.CharField(max_length=20, choices=ERP_CLASSIFICATION_CHOICES, null=True, blank=True)
    classification_completed_at = models.DateTimeField(null=True, blank=True)
    bom_completed = models.BooleanField(default=False)

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
    """A traceable lot of an item — the backbone of SQF traceability.

    A lot is created when material is *received* (raw) or *produced* (finished).
    Its genealogy links backward (which raw lots a produced lot consumed, via
    LotConsumption) and forward (which shipments it left on, via ShipmentLot),
    so any shipped case traces back to its production run and ingredients.

    `quantity` is the original lot size; `remaining_quantity` is what's left
    after consumption/shipment, so lots can be drawn down FIFO.
    """

    SOURCE_CHOICES = [
        ("received", "Received from vendor"),
        ("produced", "Produced in-house"),
        ("legacy", "Legacy / manual"),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    batch_number = models.CharField(max_length=100)  # the lot code
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.FloatField()

    # --- Lot-chain fields (all nullable so existing Batch rows/flows are intact) ---
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="legacy")
    # Raw lots point at the goods receipt they came in on.
    goods_receipt = models.ForeignKey(
        "procurement.GoodsReceipt", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="lots",
    )
    # Finished lots point at the production order that made them.
    production_order = models.ForeignKey(
        "production.ProductionOrder", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="lots",
    )
    remaining_quantity = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"Lot {self.batch_number} · {self.item.name}"


class LotConsumption(models.Model):
    """Genealogy link: a production order consumed `quantity` of a raw `lot`.

    This is what makes 'trace every cookie back to its ingredients' possible —
    given a finished lot's production order, its consumed raw lots are found
    here, and each raw lot's goods_receipt names the vendor delivery.
    """
    production_order = models.ForeignKey(
        "production.ProductionOrder", on_delete=models.CASCADE, related_name="lot_consumptions"
    )
    lot = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="consumed_in")
    quantity = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.quantity} of {self.lot.batch_number} → PO#{self.production_order_id}"

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


class StockTransfer(models.Model):
    """A movement of finished/raw stock between warehouses — e.g. Mount Kisco
    plant → Milton NY pickup staging, or → upstate cold storage.

    Completing a transfer decrements the source and increments the destination
    (via the same stock primitives), and carries the item's lots along so
    traceability survives the move.
    """
    STATUS_CHOICES = [
        ("in_transit", "In transit"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    source_warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="transfers_out")
    dest_warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="transfers_in")
    quantity = models.FloatField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="in_transit")
    reference = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.quantity} {self.item.name}: {self.source_warehouse} → {self.dest_warehouse}"


class CycleCount(models.Model):
    """A physical inventory count session — the thing that replaces the
    customer's 2-hour Friday-morning reconciliation.

    An operator records counted quantities per item/warehouse; the system shows
    the variance against on-hand; posting the count writes the adjustments (via
    adjust_stock) so perpetual inventory matches reality.
    """
    STATUS_CHOICES = [
        ("open", "Open"),
        ("posted", "Posted"),
        ("cancelled", "Cancelled"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Cycle count #{self.id} @ {self.warehouse} ({self.status})"


class CycleCountLine(models.Model):
    """One item's counted quantity within a cycle count. `system_quantity` is
    snapshotted when the line is added so the variance is meaningful even if
    stock moves before posting."""
    cycle_count = models.ForeignKey(CycleCount, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    system_quantity = models.FloatField()
    counted_quantity = models.FloatField()

    @property
    def variance(self):
        return self.counted_quantity - self.system_quantity

    class Meta:
        unique_together = ("cycle_count", "item")


class QuickBooksOnboarding(models.Model):
    """Track QB onboarding status per company."""
    STATUS_CHOICES = [
        ("classification", "Awaiting Item Classification"),
        ("bom_setup", "Awaiting BOM Setup"),
        ("customer_mapping", "Awaiting Customer Mapping"),
        ("sales_config", "Awaiting Sales Configuration"),
        ("vendor_mapping", "Awaiting Vendor Mapping"),
        ("procurement_config", "Awaiting Procurement Configuration"),
        ("completed", "Completed"),
    ]

    company = models.OneToOneField(
        "accounts.Company", on_delete=models.CASCADE, related_name="qb_onboarding"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="classification")
    disclaimer_acknowledged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.company.name} - {self.status}"


class SalesQuickBooksConfig(models.Model):
    """Per-company settings for how ERP sales sync to QuickBooks.

    Captured during onboarding (customer mapping + sales config phases) and
    editable later in Settings. Drives the runtime invoice/sales-receipt push
    that is built in a later phase — this model only stores the decisions.
    """
    DOC_TYPE_CHOICES = [
        ("invoice", "Invoice (bill customer, payment later)"),
        ("sales_receipt", "Sales Receipt (customer pays immediately)"),
    ]
    SALE_TRIGGER_CHOICES = [
        ("shipment", "At shipment / dispatch"),
        ("confirmation", "At order confirmation"),
    ]

    company = models.OneToOneField(
        "accounts.Company", on_delete=models.CASCADE, related_name="sales_qb_config"
    )
    # Phase: Customer Mapping — set true once the admin has reviewed the
    # customer mapping screen and linked/created QB customers.
    customers_mapped = models.BooleanField(default=False)
    # Phase: Sale Trigger Point
    sale_trigger = models.CharField(
        max_length=20, choices=SALE_TRIGGER_CHOICES, default="shipment"
    )
    # Phase: Invoice vs Sales Receipt (business default, overridable per-sale later)
    default_doc_type = models.CharField(
        max_length=20, choices=DOC_TYPE_CHOICES, default="invoice"
    )
    # Phase: Item & Price Sync — one-time acknowledgement that the entered
    # transaction price (not the catalog default) is what gets sent to QB.
    price_disclaimer_acknowledged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.company.name} - Sales QB Config ({self.default_doc_type})"


class ProcurementQuickBooksConfig(models.Model):
    """Per-company settings for how ERP procurement syncs to QuickBooks.

    Captured during onboarding (vendor mapping + procurement config phases) and
    editable later in Settings. Mirrors SalesQuickBooksConfig on the payables
    side. Config-capture only — the runtime Bill push is a later phase.
    """
    PURCHASE_TRIGGER_CHOICES = [
        ("goods_receipt", "At goods receipt (material received into warehouse)"),
        ("po_creation", "At PO creation / approval"),
    ]
    COST_SOURCE_CHOICES = [
        ("po_price", "PO-negotiated price"),
        ("invoice_price", "Actual vendor invoice amount (if it differs from PO)"),
    ]

    company = models.OneToOneField(
        "accounts.Company", on_delete=models.CASCADE, related_name="procurement_qb_config"
    )
    # Phase: Vendor Mapping — set true once the admin has reviewed the vendor
    # mapping screen and linked/created QB vendors.
    vendors_mapped = models.BooleanField(default=False)
    # Phase: Purchase Trigger Point
    purchase_trigger = models.CharField(
        max_length=20, choices=PURCHASE_TRIGGER_CHOICES, default="goods_receipt"
    )
    # Phase: Cost source — PO price vs. actual received/vendor-invoice price.
    cost_source = models.CharField(
        max_length=20, choices=COST_SOURCE_CHOICES, default="po_price"
    )
    # Phase: Payables disclaimer — one-time ack that ERP does not manage vendor
    # payments (Bill Payments stay in QuickBooks).
    payables_disclaimer_acknowledged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.company.name} - Procurement QB Config ({self.purchase_trigger})"


class BOM(models.Model):
    """Bill of Materials for finished goods."""
    finished_good = models.OneToOneField(
        Item, on_delete=models.CASCADE, related_name="bom", limit_choices_to={"category": "finished_good"}
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"BOM for {self.finished_good.name}"


class BOMLine(models.Model):
    """Individual line item in a BOM."""
    bom = models.ForeignKey(BOM, on_delete=models.CASCADE, related_name="lines")
    raw_material = models.ForeignKey(
        Item, on_delete=models.CASCADE, limit_choices_to={"category": "raw_material"}
    )
    quantity = models.FloatField(help_text="Quantity of raw material per unit of finished good")
    unit = models.CharField(max_length=50, default="unit")
    # The unit `quantity` is expressed in. Nullable during backfill; when set,
    # stock deduction converts this into the raw material's base_unit.
    unit_of_measure = models.ForeignKey(
        "inventory.UnitOfMeasure", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        unique_together = ("bom", "raw_material")

    def quantity_in_base_unit(self):
        """The line's quantity expressed in the raw material's base_unit, so it
        can be deducted from stock correctly.

        Falls back to the raw quantity when units aren't set up yet (during
        backfill) or when the material has no base_unit — preserving today's
        behavior for un-migrated items instead of raising.
        """
        from decimal import Decimal
        from .uom import convert, UomConversionError

        line_uom = self.unit_of_measure
        base_uom = self.raw_material.base_unit
        if not line_uom or not base_uom:
            return Decimal(str(self.quantity))
        try:
            return convert(self.quantity, line_uom, base_uom)
        except UomConversionError:
            # Misconfigured units (e.g. count vs mass): don't silently corrupt
            # stock — surface the raw quantity and let validation catch it.
            return Decimal(str(self.quantity))

    def __str__(self):
        return f"{self.quantity} x {self.raw_material.name} → {self.bom.finished_good.name}"
