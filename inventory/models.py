from django.db import models

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

    class Meta:
        unique_together = ("bom", "raw_material")

    def __str__(self):
        return f"{self.quantity} x {self.raw_material.name} → {self.bom.finished_good.name}"
