from django.db import models
from django.utils import timezone
from inventory.models import Item, Warehouse
from decimal import Decimal


class Vendor(models.Model):
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, default="raw_material")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    address = models.TextField(blank=True, default="")
    rating = models.FloatField(default=0.0)
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True, default="")
    quickbooks_sync_token = models.CharField(max_length=100, blank=True, default="")
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)
    payment_terms = models.CharField(max_length=100, blank=True, default="")
    tax_id = models.CharField(max_length=100, blank=True, default="")
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

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
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    effective_date = models.DateField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("vendor", "item")
        ordering = ["vendor", "item"]

    def __str__(self):
        return f"{self.vendor.name} → {self.item.name} @ {self.currency} {self.unit_price}"


class PurchaseRequisition(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("approved", "Approved"),
        ("converted", "Converted"),
        ("cancelled", "Cancelled"),
    ]

    requisition_number = models.CharField(max_length=64, unique=True, db_index=True)
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="purchase_requisitions"
    )
    warehouse = models.ForeignKey(
        Warehouse, null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_requisitions"
    )
    production_plan = models.ForeignKey(
        "production.ProductionPlan", null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_requisitions"
    )
    sales_order = models.ForeignKey(
        "sales.SalesOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_requisitions"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.requisition_number:
            import time
            plan_id = self.production_plan_id or "0"
            ts = int(time.time() * 1000) % 1000000
            candidate = f"PR-{plan_id}-{ts}"
            while PurchaseRequisition.objects.filter(requisition_number=candidate).exists():
                ts += 1
                candidate = f"PR-{plan_id}-{ts}"
            self.requisition_number = candidate
        if self.production_plan and not self.sales_order_id and getattr(self.production_plan, 'sales_order_id', None):
            self.sales_order = self.production_plan.sales_order
        if self.production_plan and not self.company_id and getattr(self.production_plan, 'company_id', None):
            self.company = self.production_plan.company
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.requisition_number} ({self.get_status_display()})"


class PurchaseRequisitionItem(models.Model):
    requisition = models.ForeignKey(PurchaseRequisition, on_delete=models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    required_quantity = models.FloatField()
    available_quantity = models.FloatField(default=0.0)
    shortage_quantity = models.FloatField()
    uom = models.CharField(max_length=20, blank=True, default="")
    estimated_unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    notes = models.TextField(blank=True, default="")

    def __str__(self):
        return f"{self.item.name} - Required: {self.required_quantity}, Shortage: {self.shortage_quantity} {self.uom}"


class PurchaseOrder(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    requisition = models.ForeignKey(
        PurchaseRequisition, null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_orders"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expected_delivery = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=20, default="normal")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("ordered", "Ordered"),
        ("received", "Received"),
        ("cancelled", "Cancelled"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    notes = models.TextField(blank=True, default="", help_text="Internal notes; not sent to the vendor.")

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


class VendorEmail(models.Model):
    """A purchase-order email to a vendor, drafted automatically on PO creation.

    Covers one or more purchase orders: orders raised for the same vendor while
    a draft is still open are consolidated into that draft rather than each
    producing its own message.

    Delivery is not implemented. The transport-facing fields (`status`,
    `sent_at`, `sent_by`, `error_message`) exist so adding SMTP later is a
    matter of writing a sender, not migrating the schema.
    """

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("queued", "Queued"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="emails")
    purchase_orders = models.ManyToManyField(
        PurchaseOrder, related_name="emails", blank=True,
        help_text="Every order this message covers.",
    )

    to_email = models.EmailField(blank=True, default="")
    cc = models.CharField(max_length=500, blank=True, help_text="Comma-separated", default="")
    bcc = models.CharField(max_length=500, blank=True, help_text="Comma-separated", default="")
    subject = models.CharField(max_length=300, blank=True, default="")
    body_html = models.TextField(blank=True, default="")

    # Set once the user edits the body, so regenerating the draft for a newly
    # added order never discards their wording.
    body_edited = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Populated by a future SMTP transport; unused today.
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status"], name="proc_email_scope_idx"),
        ]

    def __str__(self):
        return f"{self.get_status_display()} email to {self.vendor.name}"


class VendorEmailAttachment(models.Model):
    email = models.ForeignKey(VendorEmail, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="vendor_emails/")
    filename = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.filename


class GoodsReceipt(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    received_at = models.DateTimeField(auto_now_add=True)


class Bill(models.Model):
    """Vendor invoice recorded against a purchase order (Accounts Payable)."""

    STATUS_CHOICES = [
        ("open", "Open"),
        ("partial", "Partially Paid"),
        ("paid", "Paid"),
        ("cancelled", "Cancelled"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="bills"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="bills")
    bill_number = models.CharField(max_length=100, blank=True, help_text="Vendor's invoice number")
    bill_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def balance_due(self):
        return max(Decimal("0.00"), self.total_amount - self.amount_paid)

    def apply_payment(self, amount):
        self.amount_paid += amount
        self.status = "paid" if self.amount_paid >= self.total_amount else "partial"
        self.save(update_fields=["amount_paid", "status"])

    def __str__(self):
        return f"Bill {self.bill_number or self.id} from {self.vendor.name}"


class BillLine(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    description = models.CharField(max_length=255, blank=True)
    quantity = models.FloatField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))


class VendorPayment(models.Model):
    METHOD_CHOICES = [
        ("cash", "Cash"),
        ("bank_transfer", "Bank Transfer"),
        ("card", "Card"),
        ("cheque", "Cheque"),
        ("upi", "UPI"),
        ("other", "Other"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="payments")
    bill = models.ForeignKey(
        Bill, null=True, blank=True, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_date = models.DateField(default=timezone.localdate)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="bank_transfer")
    reference = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-created_at"]

    def __str__(self):
        return f"VPMT-{self.id} (${self.amount}) to {self.vendor.name}"
