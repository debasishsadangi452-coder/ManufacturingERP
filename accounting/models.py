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
        # Validate inventory account company consistency
        inv_account_fields = [
            ("inventory_raw_material_account", self.inventory_raw_material_account),
            ("inventory_finished_goods_account", self.inventory_finished_goods_account),
            ("inventory_clearing_account", self.inventory_clearing_account),
            ("inventory_cogs_account", self.inventory_cogs_account),
            ("inventory_adjustment_account", self.inventory_adjustment_account),
            ("inventory_write_off_account", self.inventory_write_off_account),
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

