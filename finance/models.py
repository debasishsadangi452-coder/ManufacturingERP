from django.db import models
from django.conf import settings
from decimal import Decimal


class DepartmentBudget(models.Model):
    """Admin sets budget per department/role per period."""
    DEPARTMENT_CHOICES = [
        ("inventory", "Inventory"),
        ("quality", "Quality Control"),
        ("production", "Production"),
        ("maintenance", "Maintenance"),
        ("procurement", "Procurement"),
        ("logistics", "Logistics"),
        ("hr", "Human Resources"),
        ("sales", "Sales"),
        ("admin", "Administration"),
    ]
    PERIOD_CHOICES = [
        ("monthly", "Monthly"),
        ("quarterly", "Quarterly"),
        ("yearly", "Yearly"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES)
    period = models.CharField(max_length=20, choices=PERIOD_CHOICES, default="monthly")
    period_label = models.CharField(max_length=50, help_text="e.g. 'Jan 2026', 'Q1 2026'")
    total_budget = models.DecimalField(max_digits=14, decimal_places=2)
    # threshold below which no admin approval needed (as a % of budget or fixed amount)
    auto_approve_limit = models.DecimalField(
        max_digits=12, decimal_places=2,
        default=Decimal("5000.00"),
        help_text="Expense requests below this amount are auto-approved if within budget."
    )
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="budgets_set"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("company", "department", "period_label")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_department_display()} – {self.period_label} (${self.total_budget:,.2f})"

    @property
    def spent(self):
        approved = self.expense_requests.filter(
            status__in=["approved", "auto_approved"]
        ).aggregate(total=models.Sum("amount"))["total"] or Decimal("0")
        return approved

    @property
    def remaining(self):
        return self.total_budget - self.spent

    @property
    def utilization_pct(self):
        if self.total_budget == 0:
            return 0
        return float(self.spent / self.total_budget * 100)


class ExpenseRequest(models.Model):
    """Any user can submit an expense request; approval logic based on amount & budget."""
    CATEGORY_CHOICES = [
        ("raw_material", "Raw Materials"),
        ("equipment", "Equipment"),
        ("maintenance", "Maintenance & Repairs"),
        ("utilities", "Utilities"),
        ("packaging", "Packaging"),
        ("labor", "Labor / Contractor"),
        ("transport", "Transport / Logistics"),
        ("software", "Software / IT"),
        ("safety", "Safety & Compliance"),
        ("training", "Training"),
        ("miscellaneous", "Miscellaneous"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("auto_approved", "Auto Approved"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    budget = models.ForeignKey(
        DepartmentBudget, on_delete=models.SET_NULL,
        null=True, related_name="expense_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="expense_requests"
    )
    vendor = models.CharField(max_length=200, blank=True)
    reference_number = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    notes = models.TextField(blank=True, help_text="Admin notes / rejection reason")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="expense_reviews"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} – ${self.amount} ({self.status})"

    def save(self, *args, **kwargs):
        """Auto-approve if amount <= auto_approve_limit AND remaining budget is sufficient."""
        if self.pk is None and self.budget and self.status == "pending":
            if (self.amount <= self.budget.auto_approve_limit and
                    self.amount <= self.budget.remaining):
                self.status = "auto_approved"
        super().save(*args, **kwargs)


class OperationalCost(models.Model):
    """Actual recorded operational costs (can be linked to expense requests or entered directly)."""
    COST_TYPE_CHOICES = [
        ("fixed", "Fixed Cost"),
        ("variable", "Variable Cost"),
        ("one_time", "One-Time Cost"),
    ]
    DEPARTMENT_CHOICES = DepartmentBudget.DEPARTMENT_CHOICES

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    title = models.CharField(max_length=300)
    cost_type = models.CharField(max_length=20, choices=COST_TYPE_CHOICES)
    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField()
    vendor = models.CharField(max_length=200, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True)
    expense_request = models.OneToOneField(
        ExpenseRequest, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="operational_cost"
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="recorded_costs"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.title} – ${self.amount} ({self.date})"


class PayrollRecord(models.Model):
    """Monthly payroll for each user/employee."""
    PAY_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processed", "Processed"),
        ("paid", "Paid"),
        ("on_hold", "On Hold"),
    ]

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="payroll_records"
    )
    period_label = models.CharField(max_length=50, help_text="e.g. 'January 2026'")
    basic_salary = models.DecimalField(max_digits=10, decimal_places=2)
    allowances = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    overtime_pay = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    deductions = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    net_salary = models.DecimalField(max_digits=10, decimal_places=2)
    pay_status = models.CharField(max_length=20, choices=PAY_STATUS_CHOICES, default="pending")
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="payroll_processed"
    )
    payment_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("employee", "period_label")
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payroll: {self.employee.username} – {self.period_label} (${self.net_salary})"

    def save(self, *args, **kwargs):
        """Auto-calculate net salary."""
        self.net_salary = (
            self.basic_salary + self.allowances + self.overtime_pay -
            self.deductions - self.tax
        )
        super().save(*args, **kwargs)


class FinancialSummary(models.Model):
    """Monthly financial snapshot for reporting (admin creates/updates)."""
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    period_label = models.CharField(max_length=50)
    total_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    total_expenses = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    total_payroll = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    cogs = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"), verbose_name="COGS")
    net_profit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="financial_summaries"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Financial Summary – {self.period_label}"
