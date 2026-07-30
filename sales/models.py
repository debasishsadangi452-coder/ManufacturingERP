from django.db import models
from django.utils import timezone
from inventory.models import Item, Warehouse


# -------------------------------------------------
# 👤 Customer
# -------------------------------------------------

class Customer(models.Model):
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)
    payment_terms = models.CharField(max_length=100, blank=True)
    tax_status = models.CharField(max_length=100, blank=True)
    balance_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return self.name


# -------------------------------------------------
# 🧾 Sales Order
# -------------------------------------------------

class SalesOrder(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    # Mirrored to QuickBooks as an Estimate (QBO has no Sales Order entity)
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)

    STATUS_CHOICES = [
        # "draft" = created from an inbound email and awaiting human confirmation.
        # Draft orders are excluded from the QuickBooks push until confirmed.
        ("draft", "Draft"),
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("shipped", "Shipped"),
        ("delivered", "Delivered"),
        ("cancelled", "Cancelled"),
    ]

    SOURCE_CHOICES = [
        ("manual", "Manual entry"),
        ("email", "Email inbox"),
    ]

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")

    def __str__(self):
        return f"SO-{self.id}"

class SalesOrderItem(models.Model):
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    quantity = models.FloatField()
    shipped_quantity = models.FloatField(default=0.0)

# -------------------------------------------------
# 💰 Invoice & Payments (Accounts Receivable)
# -------------------------------------------------

class Invoice(models.Model):
    STATUS_CHOICES = [
        ("open", "Open"),
        ("partial", "Partially Paid"),
        ("paid", "Paid"),
        ("cancelled", "Cancelled"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="invoices")
    invoice_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def balance_due(self):
        return self.total_amount - self.amount_paid

    def apply_payment(self, amount):
        self.amount_paid += amount
        self.status = "paid" if self.amount_paid >= self.total_amount else "partial"
        self.save(update_fields=["amount_paid", "status"])

    def __str__(self):
        return f"INV-{self.id}"


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    description = models.CharField(max_length=255, blank=True)
    quantity = models.FloatField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)


class CustomerPayment(models.Model):
    METHOD_CHOICES = [
        ("cash", "Cash"),
        ("bank_transfer", "Bank Transfer"),
        ("card", "Card"),
        ("upi", "UPI"),
        ("cheque", "Cheque"),
        ("other", "Other"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="payments")
    invoice = models.ForeignKey(
        Invoice, null=True, blank=True, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_date = models.DateField(default=timezone.localdate)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="bank_transfer")
    reference = models.CharField(max_length=100, blank=True)
    quickbooks_id = models.CharField(max_length=100, blank=True, db_index=True)
    quickbooks_sync_token = models.CharField(max_length=100, blank=True)
    quickbooks_last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payment {self.amount} from {self.customer.name}"


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


class ShipmentLot(models.Model):
    """Genealogy link: which finished lot (and how much) left on a shipment.

    Closes the SQF chain — from a customer's shipment, you can list the exact
    finished lots delivered, and from each lot trace back through its production
    order to the raw lots consumed. Kept permanently for recall/audit.
    """
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="shipment_lots")
    lot = models.ForeignKey("inventory.Batch", on_delete=models.PROTECT, related_name="shipped_on")
    quantity = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.quantity} of {self.lot.batch_number} on shipment {self.shipment_id}"


class InboundOrderEmail(models.Model):
    """An order email received in the shared inbox, its AI-extracted parse, and
    the draft SalesOrder it produced. Kept as a permanent audit trail (SQF).

    The pipeline: email arrives → AI extracts customer + line items + confidence
    → a draft SalesOrder is created → a human reviews and confirms. Nothing
    syncs to QuickBooks or production until the draft is confirmed.
    """

    STATUS_CHOICES = [
        ("received", "Received"),          # ingested, not yet parsed
        ("parsed", "Parsed"),              # AI extracted, draft created, awaiting review
        ("needs_attention", "Needs attention"),  # low confidence / could not match
        ("confirmed", "Confirmed"),        # human confirmed → order promoted
        ("failed", "Failed"),              # parse/match error
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    sender = models.EmailField(blank=True)
    subject = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    raw_body = models.TextField(blank=True)
    # Full AI extraction result (customer, lines, pickup date, per-field notes).
    parsed_data = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(null=True, blank=True, help_text="0–1 extraction confidence")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="received")
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="inbound_emails"
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"Inbound order from {self.sender} ({self.status})"
