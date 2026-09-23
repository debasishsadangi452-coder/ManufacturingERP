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

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Accounting Settings ({self.company.name})"
