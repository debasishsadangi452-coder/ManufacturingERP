"""
Indian (Tally-style) accounting - independent tables (all prefixed ``inacc_``).

Nothing here touches the legacy ``accounting`` app's tables. A company opts in
through ``AccountingMode``; companies without a row keep the legacy system.
Posted vouchers are immutable: corrections go through reversal vouchers.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class AccountingMode(models.Model):
    """Which accounting system a company chose at onboarding."""
    MODE_CHOICES = [("standard", "Existing accounting system"), ("indian", "Indian accounting system")]

    company = models.OneToOneField("accounts.Company", on_delete=models.CASCADE, related_name="accounting_mode")
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    chosen_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    chosen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inacc_accounting_mode"


class IndianCompanyProfile(models.Model):
    company = models.OneToOneField("accounts.Company", on_delete=models.CASCADE, related_name="indian_profile")
    legal_name = models.CharField(max_length=200, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    state_code = models.CharField(max_length=2, help_text="GST state code, e.g. 27 = Maharashtra")
    address = models.TextField(blank=True)
    registration_type = models.CharField(
        max_length=15, default="regular",
        choices=[("regular", "Regular"), ("composition", "Composition"), ("unregistered", "Unregistered")],
    )
    fy_start_month = models.PositiveSmallIntegerField(default=4)
    currency = models.CharField(max_length=3, default="INR")
    decimal_places = models.PositiveSmallIntegerField(default=2)
    costing_method = models.CharField(max_length=20, default="standard",
                                      choices=[("standard", "Standard (ERP cost)"), ("weighted_average", "Weighted average")])
    cogs_on = models.CharField(max_length=10, default="invoice", choices=[("invoice", "Invoice"), ("dispatch", "Dispatch")])
    posting_enabled = models.BooleanField(default=False, help_text="Set once mandatory ledgers are mapped")
    require_bill_gst = models.BooleanField(
        default=False, help_text="Hold vendor bills until their GST details are entered (needed to claim input credit)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inacc_company_profile"


class FinancialYear(models.Model):
    STATUS = [("open", "Open"), ("closed", "Closed")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="inacc_fiscal_years")
    name = models.CharField(max_length=20)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS, default="open")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "inacc_financial_year"
        unique_together = [("company", "name")]
        ordering = ["-start_date"]
        constraints = [models.CheckConstraint(condition=Q(end_date__gt=models.F("start_date")), name="inacc_fy_dates")]


class AccountingPeriod(models.Model):
    STATUS = [("open", "Open"), ("locked", "Locked")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    financial_year = models.ForeignKey(FinancialYear, on_delete=models.CASCADE, related_name="periods")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS, default="open")
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "inacc_period"
        unique_together = [("company", "start_date")]
        ordering = ["start_date"]


class AccountGroup(models.Model):
    """Tally-style group. ``nature`` drives report placement."""
    NATURE = [("asset", "Asset"), ("liability", "Liability"), ("income", "Income"), ("expense", "Expense")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="inacc_groups")
    name = models.CharField(max_length=120)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    nature = models.CharField(max_length=10, choices=NATURE)
    category = models.CharField(max_length=30, blank=True, help_text="direct/indirect, current/fixed, ...")
    is_system = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "inacc_account_group"
        unique_together = [("company", "name")]
        ordering = ["display_order", "name"]

    def clean(self):
        if self.parent_id and self.parent.company_id != self.company_id:
            raise ValidationError("Parent group must belong to the same company.")

    def __str__(self):
        return self.name


class Ledger(models.Model):
    CONTROL = [
        ("", "None"), ("receivable", "Receivables"), ("payable", "Payables"),
        ("cash", "Cash"), ("bank", "Bank"), ("inventory", "Inventory"), ("tax", "Tax"),
    ]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="inacc_ledgers")
    group = models.ForeignKey(AccountGroup, on_delete=models.PROTECT, related_name="ledgers")
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=150)
    control_type = models.CharField(max_length=12, choices=CONTROL, blank=True, default="")
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    opening_side = models.CharField(max_length=2, choices=[("Dr", "Dr"), ("Cr", "Cr")], default="Dr")
    opening_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    requires_reconciliation = models.BooleanField(default=False)
    # Links to existing masters - no duplicate customer/supplier database
    customer = models.ForeignKey("sales.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("procurement.Vendor", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    gstin = models.CharField(max_length=15, blank=True)
    state_code = models.CharField(max_length=2, blank=True)
    state_source = models.CharField(max_length=10, blank=True, help_text="gstin | address | manual")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inacc_ledger"
        unique_together = [("company", "code")]
        ordering = ["code"]
        constraints = [models.CheckConstraint(condition=Q(opening_balance__gte=0), name="inacc_ledger_open_nonneg")]

    def clean(self):
        if self.group_id and self.group.company_id != self.company_id:
            raise ValidationError("Group must belong to the same company.")

    @property
    def nature(self):
        return self.group.nature

    def __str__(self):
        return f"{self.code} {self.name}"


class LedgerMapping(models.Model):
    """Default posting ledgers (sales, output GST, round-off, ...)."""
    KEYS = [
        "sales", "sales_returns", "purchases", "receivable", "payable", "cash", "bank",
        "output_cgst", "output_sgst", "output_igst", "input_cgst", "input_sgst", "input_igst",
        "round_off", "discount", "freight", "raw_material", "wip", "finished_goods", "cogs",
        "grni", "scrap", "labour_absorbed", "overhead_absorbed", "retained_earnings",
    ]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=30)
    ledger = models.ForeignKey(Ledger, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "inacc_ledger_mapping"
        unique_together = [("company", "key")]


class TaxCode(models.Model):
    TREATMENT = [("taxable", "Taxable"), ("exempt", "Exempt"), ("nil", "Nil rated"), ("zero", "Zero rated"), ("non_gst", "Non-GST")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="inacc_tax_codes")
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=100)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2)
    treatment = models.CharField(max_length=10, choices=TREATMENT, default="taxable")
    hsn_sac = models.CharField(max_length=10, blank=True)
    itc_eligible = models.BooleanField(default=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "inacc_tax_code"
        unique_together = [("company", "code", "effective_from")]
        constraints = [models.CheckConstraint(condition=Q(gst_rate__gte=0, gst_rate__lte=100), name="inacc_tax_rate_range")]


class VoucherSequence(models.Model):
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    financial_year = models.ForeignKey(FinancialYear, on_delete=models.CASCADE, related_name="+")
    voucher_type = models.CharField(max_length=20)
    prefix = models.CharField(max_length=10, blank=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "inacc_voucher_sequence"
        unique_together = [("company", "financial_year", "voucher_type")]


class Voucher(models.Model):
    TYPES = [
        ("journal", "Journal"), ("payment", "Payment"), ("receipt", "Receipt"), ("contra", "Contra"),
        ("sales", "Sales Invoice"), ("purchase", "Purchase Bill"),
        ("credit_note", "Credit Note"), ("debit_note", "Debit Note"),
    ]
    STATUS = [
        ("draft", "Draft"), ("submitted", "Submitted"), ("approved", "Approved"),
        ("posted", "Posted"), ("reversed", "Reversed"), ("cancelled", "Cancelled"),
    ]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="inacc_vouchers")
    financial_year = models.ForeignKey(FinancialYear, on_delete=models.PROTECT, related_name="vouchers")
    period = models.ForeignKey(AccountingPeriod, on_delete=models.PROTECT, null=True, blank=True, related_name="vouchers")
    voucher_type = models.CharField(max_length=20, choices=TYPES)
    voucher_number = models.CharField(max_length=40, blank=True)
    voucher_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS, default="draft")
    narration = models.TextField(blank=True)
    reference = models.CharField(max_length=100, blank=True)
    party_ledger = models.ForeignKey(Ledger, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    place_of_supply = models.CharField(max_length=2, blank=True)
    # Source traceability / idempotency
    source_module = models.CharField(max_length=30, blank=True)
    source_document_type = models.CharField(max_length=30, blank=True)
    source_document_id = models.CharField(max_length=40, blank=True)
    source_event = models.CharField(max_length=30, blank=True)
    idempotency_key = models.CharField(max_length=80, null=True, blank=True)
    supplier_invoice_no = models.CharField(max_length=60, blank=True)
    due_date = models.DateField(null=True, blank=True)
    is_opening = models.BooleanField(default=False, help_text="Opening outstanding document: carries no ledger lines")
    against = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="notes")
    reversal_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by")
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inacc_voucher"
        ordering = ["-voucher_date", "-id"]
        indexes = [
            models.Index(fields=["company", "voucher_date"]),
            models.Index(fields=["company", "source_module", "source_document_type", "source_document_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "financial_year", "voucher_type", "voucher_number"],
                condition=~Q(voucher_number=""), name="inacc_voucher_number_uq"),
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False), name="inacc_voucher_idem_uq"),
            # one live posting per source event (reversed/cancelled vouchers free the slot)
            models.UniqueConstraint(
                fields=["company", "source_module", "source_document_type", "source_document_id", "source_event"],
                condition=~Q(source_document_id="") & Q(status__in=["draft", "submitted", "approved", "posted"]),
                name="inacc_voucher_source_uq"),
        ]

    def save(self, *args, **kwargs):
        allow = self.__dict__.pop("_allow_status_change", False)  # one-shot bypass for engine transitions
        if self.pk and not allow:
            old = Voucher.objects.filter(pk=self.pk).values("status").first()
            if old and old["status"] in ("posted", "reversed"):
                raise ValidationError("Posted vouchers are immutable; use reversal.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status in ("posted", "reversed"):
            raise ValidationError("Posted vouchers cannot be deleted.")
        super().delete(*args, **kwargs)


class VoucherLine(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name="lines")
    line_number = models.PositiveIntegerField()
    ledger = models.ForeignKey(Ledger, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    narration = models.CharField(max_length=255, blank=True)
    # Denormalised for fast, tenant-safe reporting
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    voucher_date = models.DateField()
    is_posted = models.BooleanField(default=False)

    class Meta:
        db_table = "inacc_voucher_line"
        ordering = ["voucher_id", "line_number"]
        unique_together = [("voucher", "line_number")]
        indexes = [models.Index(fields=["company", "ledger", "voucher_date"])]
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0) & Q(credit__gte=0), name="inacc_line_nonneg"),
            models.CheckConstraint(
                condition=(Q(debit__gt=0, credit=0) | Q(credit__gt=0, debit=0)), name="inacc_line_one_side"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_allow_post_flag", False) \
                and VoucherLine.objects.filter(pk=self.pk, is_posted=True).exists():
            raise ValidationError("Posted voucher lines are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_posted:
            raise ValidationError("Posted voucher lines cannot be deleted.")
        super().delete(*args, **kwargs)


class VoucherTaxLine(models.Model):
    """GST snapshot persisted at posting time."""
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name="tax_lines")
    tax_code = models.ForeignKey(TaxCode, null=True, on_delete=models.PROTECT, related_name="+")
    line_ref = models.CharField(max_length=40, blank=True)
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2)
    component = models.CharField(max_length=5, choices=[("CGST", "CGST"), ("SGST", "SGST"), ("IGST", "IGST")])
    applied_rate = models.DecimalField(max_digits=5, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2)
    place_of_supply = models.CharField(max_length=2)
    seller_state = models.CharField(max_length=2)
    treatment = models.CharField(max_length=10)
    itc_eligible = models.BooleanField(default=True)
    snapshot = models.JSONField(default=dict)

    class Meta:
        db_table = "inacc_voucher_tax_line"


class AuditLog(models.Model):
    """Append-only at the application level."""
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL, related_name="+")
    entity_type = models.CharField(max_length=30)
    entity_id = models.CharField(max_length=40)
    action = models.CharField(max_length=30)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inacc_audit_log"
        ordering = ["-timestamp", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Audit logs are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit logs cannot be deleted.")


class PostingEvent(models.Model):
    """Outcome of an operational event routed into the Indian ledger (replayable)."""
    STATUS = [("posted", "Posted"), ("skipped", "Skipped"), ("failed", "Failed")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    event = models.CharField(max_length=40)
    source_id = models.CharField(max_length=40)
    status = models.CharField(max_length=10, choices=STATUS)
    message = models.TextField(blank=True)
    voucher = models.ForeignKey(Voucher, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    attempts = models.PositiveIntegerField(default=1)
    payload = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inacc_posting_event"
        unique_together = [("company", "event", "source_id")]
        ordering = ["-updated_at"]


class BillTaxDetail(models.Model):
    """GST breakup for an ERP vendor bill (the procurement Bill itself carries none)."""
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    bill = models.OneToOneField("procurement.Bill", on_delete=models.PROTECT, related_name="indian_tax")
    tax_code = models.ForeignKey(TaxCode, on_delete=models.PROTECT, related_name="+")
    taxable_value = models.DecimalField(max_digits=18, decimal_places=2)
    supplier_state = models.CharField(max_length=2, help_text="Supplier GST state code (place of supply is the company state)")
    itc_eligible = models.BooleanField(default=True)
    derived = models.BooleanField(default=False, help_text="Worked out from the PO and received goods, not typed in")
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inacc_bill_tax_detail"
        constraints = [models.CheckConstraint(condition=Q(taxable_value__gt=0), name="inacc_billtax_positive")]


class Allocation(models.Model):
    """Settles part of an invoice/bill with a receipt/payment. Never deleted; released allocations stay for audit."""
    STATUS = [("active", "Active"), ("released", "Released")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    payment_voucher = models.ForeignKey(Voucher, on_delete=models.PROTECT, related_name="allocations_made")
    invoice_voucher = models.ForeignKey(Voucher, on_delete=models.PROTECT, related_name="allocations_received")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    allocation_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS, default="active")
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    release_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "inacc_allocation"
        indexes = [models.Index(fields=["company", "invoice_voucher", "status"]),
                   models.Index(fields=["company", "payment_voucher", "status"])]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="inacc_alloc_positive")]


class ImportBatch(models.Model):
    """One uploaded file: parsed rows and row-level errors are kept for preview and audit."""
    STATUS = [("preview", "Preview"), ("committed", "Committed")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=30, default="opening_balances")
    status = models.CharField(max_length=10, choices=STATUS, default="preview")
    source_file = models.CharField(max_length=200)
    source_system = models.CharField(max_length=30, default="csv")
    file_hash = models.CharField(max_length=64)
    rows = models.JSONField(default=list)
    errors = models.JSONField(default=list, help_text="[{row, message}]")
    warnings = models.JSONField(default=list, blank=True)
    total_debit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    total_credit = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    committed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "inacc_import_batch"
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["company", "file_hash"], condition=Q(status="committed"),
                                               name="inacc_import_file_once")]


class InventoryMovement(models.Model):
    """Quantity and value in/out of stock, tied to the voucher that booked it (voided if that voucher is reversed)."""
    KINDS = [("opening", "Opening"), ("grn", "Goods receipt"), ("reject", "Rejected receipt"),
             ("production_out", "Consumed in production"), ("production_in", "Produced"), ("cogs", "Cost of sales")]
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    item = models.ForeignKey("inventory.Item", on_delete=models.PROTECT, related_name="+")
    qty = models.DecimalField(max_digits=18, decimal_places=4)
    value = models.DecimalField(max_digits=18, decimal_places=2)
    kind = models.CharField(max_length=20, choices=KINDS)
    voucher = models.ForeignKey(Voucher, null=True, blank=True, on_delete=models.PROTECT, related_name="movements")
    movement_date = models.DateField()
    voided = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inacc_inventory_movement"
        indexes = [models.Index(fields=["company", "item", "voided"])]


class ProductionPosting(models.Model):
    """What has already been booked for a production order, so later completions post only the difference."""
    company = models.ForeignKey("accounts.Company", on_delete=models.CASCADE, related_name="+")
    order_id = models.PositiveIntegerField()
    good_qty = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal("0"))
    scrap_qty = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal("0"))
    material = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    labour = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    overhead = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    runs = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "inacc_production_posting"
        unique_together = [("company", "order_id")]
