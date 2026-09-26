from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class FiscalYear(models.Model):
    """Defines a corporate fiscal year for accounting periods and reporting."""
    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="fiscal_years",
        help_text="Tenant company that owns this fiscal year."
    )
    name = models.CharField(max_length=50, help_text="e.g. FY 2026, 2026-2027")
    start_date = models.DateField(help_text="First day of the fiscal year")
    end_date = models.DateField(help_text="Last day of the fiscal year")
    is_closed = models.BooleanField(
        default=False,
        help_text="Indicates whether the fiscal year is closed for journal postings."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]
        unique_together = [("company", "name")]

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValidationError({"end_date": _("Fiscal year end date must be after the start date.")})

        # Check for overlapping fiscal years within the same company
        if self.company_id and self.start_date and self.end_date:
            overlaps = FiscalYear.objects.filter(
                company_id=self.company_id,
                start_date__lte=self.end_date,
                end_date__gte=self.start_date,
            )
            if self.pk:
                overlaps = overlaps.exclude(pk=self.pk)
            if overlaps.exists():
                raise ValidationError(_("Fiscal year overlaps with an existing fiscal year for this company."))

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        status = "Closed" if self.is_closed else "Open"
        return f"{self.name} ({self.start_date} to {self.end_date}) [{status}]"


class AccountingPeriod(models.Model):
    """A monthly, quarterly, or custom period inside a fiscal year."""
    STATUS_CHOICES = [
        ("open", "Open"),
        ("closed", "Closed"),
        ("locked", "Locked"),
    ]

    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="accounting_periods"
    )
    fiscal_year = models.ForeignKey(
        FiscalYear,
        on_delete=models.CASCADE,
        related_name="periods"
    )
    period_number = models.PositiveSmallIntegerField(
        help_text="Sequential period number within the fiscal year (1-12/13)"
    )
    name = models.CharField(max_length=50, help_text="e.g. Jan 2026, Q1 2026")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="open",
        help_text="Open = accepts postings, Locked = temporary freeze, Closed = finalized."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["fiscal_year__start_date", "period_number"]
        unique_together = [
            ("company", "fiscal_year", "period_number"),
            ("company", "fiscal_year", "name"),
        ]

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValidationError({"end_date": _("Period end date must be after the start date.")})

        if self.fiscal_year_id and self.start_date and self.end_date:
            fy = self.fiscal_year
            if self.start_date < fy.start_date or self.end_date > fy.end_date:
                raise ValidationError(_("Period date range must be entirely within the parent fiscal year range."))

            # Ensure company matches fiscal_year company
            if self.company_id and self.company_id != fy.company_id:
                raise ValidationError(_("Period company must match the fiscal year's company."))

    def save(self, *args, **kwargs):
        if self.fiscal_year_id and not self.company_id:
            self.company = self.fiscal_year.company
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.fiscal_year.name} - #{self.period_number} {self.name} ({self.status.upper()})"


class AccountType(models.Model):
    """Categorizes accounts into core financial statement classes."""
    CATEGORY_CHOICES = [
        ("asset", "Asset"),
        ("liability", "Liability"),
        ("equity", "Equity"),
        ("revenue", "Revenue"),
        ("expense", "Expense"),
    ]
    NORMAL_BALANCE_CHOICES = [
        ("debit", "Debit"),
        ("credit", "Credit"),
    ]

    name = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    normal_balance = models.CharField(max_length=10, choices=NORMAL_BALANCE_CHOICES)
    code_prefix = models.CharField(max_length=10, blank=True, help_text="e.g. 10 for Cash & Bank, 20 for Payables")
    description = models.TextField(blank=True)
    is_system = models.BooleanField(
        default=False,
        help_text="System-defined standard account types cannot be deleted."
    )

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_category_display()} - Normal {self.get_normal_balance_display()})"


class Account(models.Model):
    """Chart of Accounts node, supporting arbitrary hierarchy (parent/children)."""
    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="accounts"
    )
    code = models.CharField(
        max_length=30,
        help_text="Unique account code per company, e.g. 1010, 1020, 2010"
    )
    name = models.CharField(max_length=150)
    account_type = models.ForeignKey(
        AccountType,
        on_delete=models.PROTECT,
        related_name="accounts"
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
        help_text="Parent account for hierarchical chart of accounts."
    )
    description = models.TextField(blank=True)
    currency = models.CharField(max_length=10, default="USD")
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive accounts cannot be selected in new transactions."
    )
    is_reconciled = models.BooleanField(
        default=False,
        help_text="Flag indicating whether this account undergoes periodic reconciliation."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        unique_together = [("company", "code")]

    def clean(self):
        super().clean()
        if self.parent_id:
            # Cannot be own parent
            if self.pk and self.parent_id == self.pk:
                raise ValidationError({"parent": _("An account cannot be its own parent.")})

            # Parent must belong to same company
            if self.company_id and self.parent.company_id != self.company_id:
                raise ValidationError({"parent": _("Parent account must belong to the same company.")})

            # Check circular references
            curr = self.parent
            visited = {self.pk} if self.pk else set()
            while curr is not None:
                if curr.pk in visited:
                    raise ValidationError({"parent": _("Circular reference detected in account hierarchy.")})
                visited.add(curr.pk)
                curr = curr.parent

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_header(self):
        """Header accounts have child sub-accounts."""
        return self.children.exists()

    def __str__(self):
        return f"{self.code} - {self.name}"


class AccountingSettings(models.Model):
    """Company-level accounting defaults and lock dates."""
    company = models.OneToOneField(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="accounting_settings"
    )
    default_currency = models.CharField(max_length=10, default="USD")
    current_fiscal_year = models.ForeignKey(
        FiscalYear,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )
    retained_earnings_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )
    lock_date = models.DateField(
        null=True,
        blank=True,
        help_text="Transactions on or prior to this date cannot be created, modified or deleted."
    )
    allow_direct_posting_to_parent_accounts = models.BooleanField(
        default=False,
        help_text="If false, entries can only be posted to leaf (non-parent) accounts."
    )
    # Section #14 — Inventory Accounting Policy Configuration
    inventory_accounting_enabled = models.BooleanField(
        default=True,
        help_text="Enable or disable automated/subledger inventory accounting."
    )
    inventory_costing_method = models.CharField(
        max_length=20,
        default="fifo",
        choices=[
            ("fifo", "FIFO (First In, First Out)"),
            ("standard", "Standard Cost"),
            ("average", "Weighted Average"),
        ],
        help_text="Configured inventory costing valuation policy."
    )
    inventory_raw_material_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf asset account for Raw Materials inventory (e.g. 1210)"
    )
    inventory_finished_goods_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf asset account for Finished Goods inventory (e.g. 1230)"
    )
    inventory_clearing_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Clearing/offset account for vendor receipts (e.g. 2010 AP or 2020 GRNI)"
    )
    inventory_cogs_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Offset account for inventory issues/consumption (e.g. 5010 Direct Materials Consumed)"
    )
    inventory_adjustment_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Offset account for cycle count and manual inventory adjustments (e.g. 5090 Inventory Adjustments)"
    )
    inventory_write_off_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Expense account for damaged/expired inventory write-offs (e.g. 6520 Inventory Write-off)"
    )
    # Section #15 — Manufacturing Accounting Policy Configuration
    manufacturing_accounting_enabled = models.BooleanField(
        default=True,
        help_text="Enable or disable automated/subledger manufacturing accounting."
    )
    wip_accounting_enabled = models.BooleanField(
        default=True,
        help_text="Track Work-In-Process (WIP) asset balance during active production."
    )
    labor_accounting_enabled = models.BooleanField(
        default=False,
        help_text="Capitalize direct labor into WIP costs."
    )
    overhead_accounting_enabled = models.BooleanField(
        default=False,
        help_text="Apply manufacturing overhead into WIP costs."
    )
    scrap_accounting_enabled = models.BooleanField(
        default=True,
        help_text="Account for production scrap and rejected batches."
    )
    variance_accounting_enabled = models.BooleanField(
        default=True,
        help_text="Calculate and record manufacturing variances upon order completion."
    )
    labor_rate_per_unit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Standard direct labor cost per finished unit produced."
    )
    overhead_rate_per_unit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Standard applied manufacturing overhead per finished unit produced."
    )
    manufacturing_wip_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Asset account for Work-In-Progress (e.g. 1220 WIP)"
    )
    manufacturing_labor_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Clearing or expense account for direct labor (e.g. 2100 Accrued Payroll or 5100 Direct Labor)"
    )
    manufacturing_overhead_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Clearing or applied account for factory overhead (e.g. 5040 Overhead Applied or 5200 Factory Utilities)"
    )
    manufacturing_scrap_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Expense account for production scrap and defectives (e.g. 5080 Scrap Loss or 6520 Inventory Loss)"
    )
    manufacturing_variance_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Account for manufacturing cost variances (e.g. 5090 Manufacturing Variance)"
    )
    # Section #16 — Expenses Accounting Policy Configuration
    expenses_accounting_enabled = models.BooleanField(
        default=True,
        help_text="Enable or disable automated/subledger expenses accounting."
    )
    expenses_require_approval = models.BooleanField(
        default=True,
        help_text="Require approval before expenses can be posted to the General Ledger."
    )
    expenses_default_cash_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf cash account for cash expenses (e.g. 1030 Petty Cash)"
    )
    expenses_default_bank_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf bank account for bank expenses (e.g. 1010 Operating Bank Account)"
    )
    expenses_default_payable_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf AP account for vendor expenses (e.g. 2010 Accounts Payable)"
    )
    expenses_default_employee_payable_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf payable account for employee reimbursements (e.g. 2100 Accrued Payroll/Reimbursements)"
    )
    expenses_default_tax_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default leaf input tax recoverable account for expense taxes (e.g. 1310 Input Tax Recoverable)"
    )
    # Section #17 — Cash & Bank Opening Balance Equity Account
    opening_balance_equity_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Leaf equity account for opening balance offset (e.g. 3010 Common Stock / Owner Capital)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Accounting Settings"

    def clean(self):
        super().clean()
        if self.current_fiscal_year and self.company_id:
            if self.current_fiscal_year.company_id != self.company_id:
                raise ValidationError({"current_fiscal_year": _("Fiscal year must belong to this company.")})
        if self.retained_earnings_account and self.company_id:
            if self.retained_earnings_account.company_id != self.company_id:
                raise ValidationError({"retained_earnings_account": _("Retained earnings account must belong to this company.")})
        # Validate inventory and manufacturing account company consistency
        inv_account_fields = [
            ("inventory_raw_material_account", self.inventory_raw_material_account),
            ("inventory_finished_goods_account", self.inventory_finished_goods_account),
            ("inventory_clearing_account", self.inventory_clearing_account),
            ("inventory_cogs_account", self.inventory_cogs_account),
            ("inventory_adjustment_account", self.inventory_adjustment_account),
            ("inventory_write_off_account", self.inventory_write_off_account),
            ("manufacturing_wip_account", self.manufacturing_wip_account),
            ("manufacturing_labor_account", self.manufacturing_labor_account),
            ("manufacturing_overhead_account", self.manufacturing_overhead_account),
            ("manufacturing_scrap_account", self.manufacturing_scrap_account),
            ("manufacturing_variance_account", self.manufacturing_variance_account),
            ("expenses_default_cash_account", self.expenses_default_cash_account),
            ("expenses_default_bank_account", self.expenses_default_bank_account),
            ("expenses_default_payable_account", self.expenses_default_payable_account),
            ("expenses_default_employee_payable_account", self.expenses_default_employee_payable_account),
            ("expenses_default_tax_account", self.expenses_default_tax_account),
            ("opening_balance_equity_account", self.opening_balance_equity_account),
        ]
        for field_name, acc in inv_account_fields:
            if acc and self.company_id and acc.company_id != self.company_id:
                raise ValidationError({field_name: _(f"{field_name} must belong to this company.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Accounting Settings ({self.company.name})"


from decimal import Decimal
from django.utils import timezone


class JournalEntry(models.Model):
    """
    Core double-entry journal entry header.
    Encapsulates financial transactions with atomic lines, lifecycle status,
    and period/lock-date boundary enforcement.
    """
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("posted", "Posted"),
        ("reversed", "Reversed"),
    ]

    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="journal_entries",
        help_text="Tenant company that owns this journal entry."
    )
    entry_number = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Unique sequence number e.g. JE-2026-00001"
    )
    transaction_date = models.DateField(
        default=timezone.now,
        help_text="The effective accounting date of the transaction."
    )
    accounting_period = models.ForeignKey(
        AccountingPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="journal_entries",
        help_text="Accounting period this transaction falls within."
    )
    reference = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Source document or reference number (e.g. INV-1002, PO-5001)"
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Transaction narration or business purpose."
    )
    source_module = models.CharField(
        max_length=50,
        default="manual",
        help_text="Originating ERP module: manual, sales, procurement, inventory, production, etc."
    )
    source_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="PK of the source document in the originating module."
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
        db_index=True
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posted_journal_entries"
    )
    reversal_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reversals",
        help_text="Points to the original journal entry if this is a reversal."
    )
    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_journal_entries"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]
        unique_together = [("company", "entry_number")]
        indexes = [
            models.Index(fields=["company", "status", "transaction_date"]),
            models.Index(fields=["company", "entry_number"]),
        ]

    def auto_assign_period(self):
        """Auto-detects and assigns the AccountingPeriod matching transaction_date."""
        if not self.accounting_period and self.company_id and self.transaction_date:
            period = AccountingPeriod.objects.filter(
                company_id=self.company_id,
                start_date__lte=self.transaction_date,
                end_date__gte=self.transaction_date,
            ).first()
            if period:
                self.accounting_period = period

    def generate_entry_number(self):
        """Generates sequential entry number for the company: JE-YYYY-XXXXX."""
        year = self.transaction_date.year if self.transaction_date else timezone.now().year
        prefix = f"JE-{year}-"
        last_entry = JournalEntry.objects.filter(
            company_id=self.company_id,
            entry_number__startswith=prefix
        ).order_by("-entry_number").first()

        if last_entry and last_entry.entry_number.split("-")[-1].isdigit():
            last_num = int(last_entry.entry_number.split("-")[-1])
            new_num = last_num + 1
        else:
            new_num = 1

        return f"{prefix}{new_num:05d}"

    def clean(self):
        super().clean()
        self.auto_assign_period()

        # Check AccountingSettings lock date
        settings = getattr(self.company, "accounting_settings", None) if self.company_id else None
        if settings and settings.lock_date and self.transaction_date:
            if self.transaction_date <= settings.lock_date:
                raise ValidationError({
                    "transaction_date": _(
                        f"Cannot create or modify entries on or prior to the accounting lock date ({settings.lock_date})."
                    )
                })

        # Period boundary and status checks
        if self.accounting_period:
            if self.accounting_period.company_id != self.company_id:
                raise ValidationError({"accounting_period": _("Accounting period belongs to a different company.")})

            if self.status == "posted":
                if self.accounting_period.status != "open":
                    raise ValidationError({
                        "accounting_period": _(
                            f"Cannot post to an accounting period with status '{self.accounting_period.status}'."
                        )
                    })
                if self.accounting_period.fiscal_year.is_closed:
                    raise ValidationError({
                        "accounting_period": _("Cannot post to a closed fiscal year.")
                    })

            # Check transaction date within period boundaries
            if self.transaction_date and (
                self.transaction_date < self.accounting_period.start_date
                or self.transaction_date > self.accounting_period.end_date
            ):
                raise ValidationError({
                    "transaction_date": _(
                        f"Transaction date ({self.transaction_date}) is outside the accounting period range "
                        f"({self.accounting_period.start_date} to {self.accounting_period.end_date})."
                    )
                })

    def save(self, *args, **kwargs):
        if not self.entry_number and self.company_id:
            self.entry_number = self.generate_entry_number()
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def total_debit(self):
        result = self.lines.aggregate(models.Sum("debit"))["debit__sum"]
        return result or Decimal("0.00")

    @property
    def total_credit(self):
        result = self.lines.aggregate(models.Sum("credit"))["credit__sum"]
        return result or Decimal("0.00")

    @property
    def is_balanced(self):
        diff = abs(self.total_debit - self.total_credit)
        return diff < Decimal("0.005")

    def validate_double_entry(self):
        """
        Validates double entry rules for posting:
        1. Minimum 2 lines.
        2. Total debits == Total credits > 0.
        3. No unassigned or inactive accounts.
        """
        lines = list(self.lines.all())
        if len(lines) < 2:
            raise ValidationError(_("A journal entry must contain at least 2 lines (minimum 1 debit, 1 credit)."))

        tot_debit = sum(line.debit for line in lines)
        tot_credit = sum(line.credit for line in lines)

        if tot_debit <= Decimal("0.00"):
            raise ValidationError(_("Total debit amount must be greater than zero."))

        if abs(tot_debit - tot_credit) >= Decimal("0.005"):
            raise ValidationError(
                _(f"Double-entry unbalanced: Total Debits ({tot_debit:.2f}) != Total Credits ({tot_credit:.2f}). Difference: {abs(tot_debit - tot_credit):.2f}")
            )

        for line in lines:
            line.clean()

    def __str__(self):
        return f"{self.entry_number} [{self.status.upper()}] ({self.transaction_date})"


class JournalEntryLine(models.Model):
    """
    Individual debit or credit line within a double-entry JournalEntry.
    """
    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="journal_lines"
    )
    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="lines"
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="journal_lines"
    )
    line_number = models.PositiveSmallIntegerField(default=1)
    debit = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00")
    )
    credit = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=Decimal("0.00")
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Line item memo or specific reference."
    )
    is_reconciled = models.BooleanField(
        default=False,
        help_text="Whether this line item has been matched and cleared in bank reconciliation."
    )
    reconciliation = models.ForeignKey(
        "BankReconciliation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciled_journal_lines",
        help_text="Bank reconciliation cycle in which this line was cleared."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["line_number", "id"]
        indexes = [
            models.Index(fields=["company", "account"]),
            models.Index(fields=["journal_entry", "line_number"]),
        ]

    def clean(self):
        super().clean()
        if self.debit < 0 or self.credit < 0:
            raise ValidationError(_("Debit and credit amounts cannot be negative."))

        if self.debit == 0 and self.credit == 0:
            raise ValidationError(_("A line must have either a non-zero debit or credit amount."))

        if self.debit > 0 and self.credit > 0:
            raise ValidationError(_("A line cannot contain both a debit and credit amount. Use separate lines."))

        # Tenancy checks
        if self.journal_entry_id and self.company_id != self.journal_entry.company_id:
            raise ValidationError({"company": _("Line company must match journal entry company.")})

        if self.account_id:
            if self.company_id != self.account.company_id:
                raise ValidationError({"account": _("Account belongs to a different company.")})

            if not self.account.is_active:
                raise ValidationError({"account": _(f"Account '{self.account.code}' is inactive and cannot accept postings.")})

            # Check if parent posting allowed
            settings = getattr(self.company, "accounting_settings", None) if self.company_id else None
            allow_parent = settings.allow_direct_posting_to_parent_accounts if settings else False
            if not allow_parent and self.account.is_header:
                raise ValidationError({
                    "account": _(f"Cannot post directly to parent header account '{self.account.code}'. Post to a sub-account.")
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        side = f"DR {self.debit:.2f}" if self.debit > 0 else f"CR {self.credit:.2f}"
        return f"Line {self.line_number}: {self.account.code} - {side}"


# ==============================================================================
# BLUEPRINT SECTION #16 — EXPENSES-TO-ACCOUNTING MODELS
# ==============================================================================

class ExpenseCategory(models.Model):
    """
    Configurable expense categories mapped to Chart of Accounts leaf expense accounts.
    Enables automatic categorization and GL account resolution for operational expenses.
    """
    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="expense_categories",
        help_text="Tenant company owning this expense category."
    )
    code = models.CharField(max_length=50, help_text="Unique category identifier, e.g. TRAVEL, MEALS, SOFTWARE")
    name = models.CharField(max_length=150, help_text="Descriptive category name")
    description = models.TextField(blank=True, default="")
    expense_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="categorized_expenses",
        help_text="Default leaf expense account mapped to this category (e.g. 6020 Sales/Travel, 6040 Software)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("company", "code")]
        verbose_name_plural = "Expense Categories"

    def clean(self):
        super().clean()
        if self.expense_account_id and self.company_id:
            if self.expense_account.company_id != self.company_id:
                raise ValidationError({"expense_account": _("Expense account must belong to the same company.")})
            if not self.expense_account.is_active:
                raise ValidationError({"expense_account": _("Expense account must be active.")})
            if self.expense_account.is_header:
                raise ValidationError({"expense_account": _("Expense account must be a leaf account, not a header.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class Expense(models.Model):
    """
    Controlled operational source document for employee reimbursements and vendor expenses.
    Integrates receipt attachments, approval workflows, tax handling, payment sources,
    and double-entry journal postings into the General Ledger.
    """
    EXPENSE_TYPE_CHOICES = [
        ("employee", "Employee Expense"),
        ("vendor", "Vendor Expense"),
    ]
    PAYMENT_SOURCE_CHOICES = [
        ("cash", "Cash"),
        ("bank", "Bank Transfer / Card"),
        ("payable", "Payable / Reimbursement"),
    ]
    APPROVAL_STATUS_CHOICES = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]
    ACCOUNTING_STATUS_CHOICES = [
        ("not_ready", "Not Ready"),
        ("ready", "Ready for GL"),
        ("posted", "Posted to GL"),
        ("reversed", "Reversed"),
        ("failed", "Failed"),
    ]

    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="accounting_expenses",
        help_text="Tenant company context."
    )
    expense_number = models.CharField(max_length=64, db_index=True)
    expense_type = models.CharField(max_length=20, choices=EXPENSE_TYPE_CHOICES, default="employee")
    employee = models.ForeignKey(
        "workforce.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
        help_text="Employee claiming reimbursement (for employee expenses)"
    )
    vendor = models.ForeignKey(
        "procurement.Vendor",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
        help_text="Registered supplier (for vendor expenses)"
    )
    vendor_name_raw = models.CharField(max_length=255, blank=True, default="", help_text="Unregistered vendor name memo")
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
        help_text="Configured expense category defining default GL account mapping"
    )
    title = models.CharField(max_length=255, help_text="Summary title of the expense")
    description = models.TextField(blank=True, default="")
    expense_date = models.DateField(default=timezone.now)
    accounting_date = models.DateField(null=True, blank=True, help_text="Accounting/posting date for GL recognition")
    amount_before_tax = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=10, default="USD")

    payment_source = models.CharField(max_length=20, choices=PAYMENT_SOURCE_CHOICES, default="payable")
    expense_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Specific leaf expense account overriding category default"
    )
    tax_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Specific leaf input tax recoverable account"
    )
    payment_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Specific leaf cash, bank, or payable account"
    )

    approval_status = models.CharField(max_length=20, choices=APPROVAL_STATUS_CHOICES, default="draft")
    accounting_status = models.CharField(max_length=20, choices=ACCOUNTING_STATUS_CHOICES, default="not_ready")

    receipt = models.FileField(upload_to="expense_receipts/", null=True, blank=True)
    receipt_name = models.CharField(max_length=255, blank=True, default="")
    receipt_size = models.IntegerField(null=True, blank=True)
    receipt_content_type = models.CharField(max_length=100, blank=True, default="")

    notes = models.TextField(blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")

    journal_entry = models.ForeignKey(
        JournalEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
        help_text="Posted double-entry general ledger journal entry"
    )
    reversal_journal_entry = models.ForeignKey(
        JournalEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reversed_expenses",
        help_text="Offsetting reversal journal entry if reversed"
    )

    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_expenses"
    )
    submitted_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_expenses"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_expenses"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posted_expenses"
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        unique_together = [("company", "expense_number")]
        indexes = [
            models.Index(fields=["company", "expense_number"]),
            models.Index(fields=["company", "approval_status"]),
            models.Index(fields=["company", "accounting_status"]),
            models.Index(fields=["company", "expense_date"]),
        ]

    def clean(self):
        super().clean()
        if self.amount_before_tax < Decimal("0.00"):
            raise ValidationError({"amount_before_tax": _("Amount before tax cannot be negative.")})
        if self.tax_amount < Decimal("0.00"):
            raise ValidationError({"tax_amount": _("Tax amount cannot be negative.")})

        # Company Tenancy validations
        if self.employee_id and self.company_id and self.employee.company_id != self.company_id:
            raise ValidationError({"employee": _("Employee belongs to a different company.")})
        if self.vendor_id and self.company_id and self.vendor.company_id != self.company_id:
            raise ValidationError({"vendor": _("Vendor belongs to a different company.")})
        if self.category_id and self.company_id and self.category.company_id != self.company_id:
            raise ValidationError({"category": _("Expense category belongs to a different company.")})
        if self.expense_account_id and self.company_id and self.expense_account.company_id != self.company_id:
            raise ValidationError({"expense_account": _("Expense account belongs to a different company.")})
        if self.tax_account_id and self.company_id and self.tax_account.company_id != self.company_id:
            raise ValidationError({"tax_account": _("Tax account belongs to a different company.")})
        if self.payment_account_id and self.company_id and self.payment_account.company_id != self.company_id:
            raise ValidationError({"payment_account": _("Payment account belongs to a different company.")})

        # Leaf account validations
        if self.expense_account:
            if self.expense_account.is_header:
                raise ValidationError({"expense_account": _("Expense account must be a leaf account, not a header.")})
            if not self.expense_account.is_active:
                raise ValidationError({"expense_account": _("Expense account is inactive.")})

        if self.tax_account:
            if self.tax_account.is_header:
                raise ValidationError({"tax_account": _("Tax account must be a leaf account, not a header.")})
            if not self.tax_account.is_active:
                raise ValidationError({"tax_account": _("Tax account is inactive.")})

        if self.payment_account:
            if self.payment_account.is_header:
                raise ValidationError({"payment_account": _("Payment account must be a leaf account, not a header.")})
            if not self.payment_account.is_active:
                raise ValidationError({"payment_account": _("Payment account is inactive.")})

    def save(self, *args, **kwargs):
        # Synchronize total_amount
        before_tax = Decimal(str(self.amount_before_tax or 0))
        tax = Decimal(str(self.tax_amount or 0))
        self.total_amount = before_tax + tax

        # Auto-generate expense_number if missing
        if not self.expense_number:
            company_id = self.company_id or 1
            last = Expense.objects.filter(company_id=company_id).order_by("-id").first()
            seq = (last.id + 1) if last else 1
            self.expense_number = f"EXP-{company_id}-{seq:05d}"

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.expense_number} - {self.title} (${self.total_amount:.2f})"


class ExpenseAuditLog(models.Model):
    """
    Immutable audit log tracking all operational and accounting events for an expense,
    including creation, receipt attachments, approval transitions, prospective previews,
    GL journal postings, and reversals.
    """
    expense = models.ForeignKey(
        Expense,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        help_text="Parent expense record."
    )
    action = models.CharField(
        max_length=50,
        help_text="Action code, e.g. created, receipt_attached, submitted, approved, rejected, previewed, posted, reversed, cancelled"
    )
    actor = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="User executing the action"
    )
    details = models.JSONField(default=dict, blank=True, help_text="Structured event telemetry / state diff")
    notes = models.TextField(blank=True, default="", help_text="User comments, approval remarks, or rejection reason")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Audit: {self.expense.expense_number} - {self.action} at {self.created_at}"


# ==============================================================================
# BLUEPRINT SECTION #17 — CASH & BANK MODELS
# ==============================================================================

class BankAccount(models.Model):
    """
    Blueprint Section #17 — Bank Account Master.
    Represents commercial bank, checking, savings, money market, credit card,
    or petty cash vault accounts linked to Chart of Accounts leaf accounts.
    """
    ACCOUNT_TYPE_CHOICES = [
        ("checking", "Checking Account"),
        ("savings", "Savings Account"),
        ("money_market", "Money Market"),
        ("credit_card", "Credit Card"),
        ("cash", "Petty Cash / Vault"),
    ]

    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="bank_accounts",
        help_text="Tenant company owning this bank account."
    )
    account_name = models.CharField(max_length=150, help_text="Descriptive title, e.g. Operating Checking, Payroll Account")
    bank_name = models.CharField(max_length=150, help_text="Institution name, e.g. JPMorgan Chase, Silicon Valley Bank")
    account_number = models.CharField(max_length=50, help_text="Account number (masked in API/UI)")
    routing_number = models.CharField(max_length=50, blank=True, default="", help_text="ABA routing / sort code")
    swift_bic = models.CharField(max_length=50, blank=True, default="", help_text="SWIFT / BIC code for wire transfers")
    account_type = models.CharField(max_length=30, choices=ACCOUNT_TYPE_CHOICES, default="checking")
    currency = models.CharField(max_length=10, default="USD")
    gl_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="bank_accounts",
        help_text="Linked leaf Chart of Accounts asset or liability account (e.g. 1010, 1020, 1030)"
    )
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    opening_balance_date = models.DateField(null=True, blank=True)
    opening_balance_posted = models.BooleanField(default=False)
    opening_balance_journal_entry = models.ForeignKey(
        JournalEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="General Ledger journal entry for the opening balance"
    )
    reconciled_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    last_reconciliation_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["bank_name", "account_name"]
        indexes = [
            models.Index(fields=["company", "is_active"]),
            models.Index(fields=["company", "gl_account"]),
        ]

    def clean(self):
        super().clean()
        if self.gl_account_id and self.company_id:
            if self.gl_account.company_id != self.company_id:
                raise ValidationError({"gl_account": _("GL account must belong to the same company.")})
            if not self.gl_account.is_active:
                raise ValidationError({"gl_account": _("GL account must be active.")})
            if self.gl_account.is_header:
                raise ValidationError({"gl_account": _("GL account must be a leaf account, not a header.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def masked_account_number(self):
        if not self.account_number:
            return ""
        num = str(self.account_number).strip()
        if len(num) <= 4:
            return f"****{num}"
        return f"****{num[-4:]}"

    @property
    def current_gl_balance(self):
        """Calculates current General Ledger posted balance for linked GL account."""
        if not self.gl_account_id:
            return Decimal("0.00")
        lines = JournalEntryLine.objects.filter(
            company_id=self.company_id,
            account_id=self.gl_account_id,
            journal_entry__status="posted"
        )
        debit_sum = lines.aggregate(models.Sum("debit"))["debit__sum"] or Decimal("0.00")
        credit_sum = lines.aggregate(models.Sum("credit"))["credit__sum"] or Decimal("0.00")
        
        # Credit cards are liabilities (credit balance normal), asset accounts are debit normal
        if self.account_type == "credit_card" or (self.gl_account.account_type and self.gl_account.account_type.category == "liability"):
            return credit_sum - debit_sum
        return debit_sum - credit_sum

    @property
    def unreconciled_difference(self):
        return self.current_gl_balance - self.reconciled_balance

    def __str__(self):
        return f"{self.bank_name} - {self.account_name} ({self.masked_account_number})"


class BankReconciliation(models.Model):
    """
    Blueprint Section #17 — Bank Reconciliation.
    Encapsulates bank statement reconciliation cycles matching external bank statement
    balances and transactions against internal ERP General Ledger transactions.
    """
    STATUS_CHOICES = [
        ("completed", "Completed"),
        ("reopened", "Reopened"),
    ]

    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="bank_reconciliations",
        help_text="Tenant company owning this reconciliation cycle."
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.CASCADE,
        related_name="reconciliations",
        help_text="Reconciled bank account."
    )
    reconciliation_number = models.CharField(max_length=64, db_index=True)
    statement_date = models.DateField(help_text="Cut-off date of the external bank statement")
    statement_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), help_text="Ending statement balance")
    starting_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), help_text="Previous reconciled starting balance")
    reconciled_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), help_text="Target reconciled balance after reconciliation")
    difference = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"), help_text="Difference between statement and reconciled items")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="completed")
    notes = models.TextField(blank=True, default="")
    reconciled_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="User executing the reconciliation"
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-statement_date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "bank_account"]),
            models.Index(fields=["company", "statement_date"]),
        ]

    def save(self, *args, **kwargs):
        if not self.reconciliation_number and self.company_id:
            year = self.statement_date.year if self.statement_date else timezone.now().year
            prefix = f"REC-{year}-"
            last = BankReconciliation.objects.filter(
                company_id=self.company_id,
                reconciliation_number__startswith=prefix
            ).order_by("-id").first()
            seq = (last.id + 1) if last else 1
            self.reconciliation_number = f"{prefix}{seq:05d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reconciliation_number} ({self.bank_account.account_name} - {self.statement_date})"


class BankTransaction(models.Model):
    """
    Blueprint Section #17 — Bank Transaction Layer.
    Represents deposits, withdrawals, internal transfers, statement imports,
    and ERP-linked payments/receipts against a bank account.
    """
    DIRECTION_CHOICES = [
        ("inflow", "Inflow / Deposit / DR"),
        ("outflow", "Outflow / Withdrawal / CR"),
    ]
    TRANSACTION_TYPE_CHOICES = [
        ("deposit", "Deposit"),
        ("withdrawal", "Withdrawal"),
        ("transfer", "Bank Transfer"),
        ("customer_receipt", "Customer Receipt"),
        ("vendor_payment", "Vendor Payment"),
        ("expense_payment", "Expense Payment"),
        ("fee", "Bank Fee / Charge"),
        ("interest", "Interest Income"),
        ("statement_line", "Imported Statement Line"),
        ("opening_balance", "Opening Balance"),
        ("other", "Other Transaction"),
    ]
    SOURCE_CHOICES = [
        ("manual", "Manual Entry"),
        ("import", "Statement Import"),
        ("erp", "ERP Subledger Posting"),
    ]
    MATCHING_STATUS_CHOICES = [
        ("unmatched", "Unmatched"),
        ("suggested", "Suggested Match"),
        ("matched", "Matched"),
    ]
    RECONCILIATION_STATUS_CHOICES = [
        ("unreconciled", "Unreconciled"),
        ("reconciled", "Reconciled"),
    ]

    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="bank_transactions"
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.CASCADE,
        related_name="transactions"
    )
    transaction_date = models.DateField(default=timezone.now)
    value_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2, help_text="Absolute transaction amount")
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default="inflow")
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPE_CHOICES, default="deposit")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    description = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=100, blank=True, default="", help_text="Cheque #, reference or memo")
    counterparty = models.CharField(max_length=150, blank=True, default="", help_text="Payee or payer name")
    external_id = models.CharField(max_length=100, blank=True, default="", db_index=True, help_text="Unique statement transaction identifier for idempotency")

    matching_status = models.CharField(max_length=20, choices=MATCHING_STATUS_CHOICES, default="unmatched")
    reconciliation_status = models.CharField(max_length=20, choices=RECONCILIATION_STATUS_CHOICES, default="unreconciled")
    reconciliation = models.ForeignKey(
        BankReconciliation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transactions",
        help_text="Reconciliation cycle in which this transaction was cleared"
    )

    journal_entry = models.ForeignKey(
        JournalEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bank_transactions",
        help_text="Linked posted double-entry journal entry"
    )
    journal_entry_line = models.ForeignKey(
        JournalEntryLine,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bank_transactions",
        help_text="Specific GL journal line for this bank account"
    )
    transfer_counterpart = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="counterpart_transaction",
        help_text="Linked matching transaction in destination/source bank account for internal transfers"
    )
    source_document_ref = models.CharField(max_length=100, blank=True, default="", help_text="Traceability to AR/AP/Expense document, e.g. REC-001, PAY-002, EXP-003")

    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_bank_transactions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "bank_account", "reconciliation_status"]),
            models.Index(fields=["company", "transaction_date"]),
            models.Index(fields=["bank_account", "external_id"]),
        ]

    def clean(self):
        super().clean()
        if self.amount is not None and self.amount < Decimal("0.00"):
            raise ValidationError({"amount": _("Transaction amount must be positive.")})
        if self.bank_account_id and self.company_id and self.bank_account.company_id != self.company_id:
            raise ValidationError({"bank_account": _("Bank account belongs to a different company.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        symbol = "+" if self.direction == "inflow" else "-"
        return f"{self.transaction_date} [{self.get_transaction_type_display()}] {symbol}${self.amount:.2f} - {self.description or self.bank_account.account_name}"


class BankAuditLog(models.Model):
    """
    Blueprint Section #17 — Audit Trail for Cash & Bank events.
    Tracks creation, modification, opening balances, statement imports,
    reconciliations, matching, and reopenings.
    """
    company = models.ForeignKey(
        "accounts.Company",
        on_delete=models.CASCADE,
        related_name="bank_audit_logs"
    )
    bank_account = models.ForeignKey(
        BankAccount,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="audit_logs"
    )
    reconciliation = models.ForeignKey(
        BankReconciliation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs"
    )
    action = models.CharField(max_length=60, help_text="e.g. account_created, opening_balance_posted, deposit_recorded, withdrawal_recorded, transfer_recorded, statement_imported, reconciliation_completed, reconciliation_reopened")
    actor = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+"
    )
    details = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        acc = self.bank_account.account_name if self.bank_account else "All"
        return f"BankAudit: {acc} - {self.action} at {self.created_at}"



