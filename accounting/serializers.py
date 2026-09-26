from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from decimal import Decimal
from .models import (
    FiscalYear, AccountingPeriod, AccountType, Account, AccountingSettings,
    BankAccount, BankReconciliation, BankTransaction, BankAuditLog
)


class FiscalYearSerializer(serializers.ModelSerializer):
    periods_count = serializers.SerializerMethodField()

    class Meta:
        model = FiscalYear
        fields = [
            "id",
            "name",
            "start_date",
            "end_date",
            "is_closed",
            "periods_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "periods_count", "created_at", "updated_at"]

    def get_periods_count(self, obj):
        return obj.periods.count()

    def validate(self, attrs):
        start_date = attrs.get("start_date") or getattr(self.instance, "start_date", None)
        end_date = attrs.get("end_date") or getattr(self.instance, "end_date", None)
        if start_date and end_date and start_date >= end_date:
            raise serializers.ValidationError({"end_date": "Fiscal year end date must be after the start date."})
        return attrs


class AccountingPeriodSerializer(serializers.ModelSerializer):
    fiscal_year_name = serializers.ReadOnlyField(source="fiscal_year.name")

    class Meta:
        model = AccountingPeriod
        fields = [
            "id",
            "fiscal_year",
            "fiscal_year_name",
            "period_number",
            "name",
            "start_date",
            "end_date",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "fiscal_year_name", "created_at", "updated_at"]

    def validate(self, attrs):
        start_date = attrs.get("start_date") or getattr(self.instance, "start_date", None)
        end_date = attrs.get("end_date") or getattr(self.instance, "end_date", None)
        fy = attrs.get("fiscal_year") or getattr(self.instance, "fiscal_year", None)

        if start_date and end_date and start_date >= end_date:
            raise serializers.ValidationError({"end_date": "Period end date must be after start date."})

        if fy and start_date and end_date:
            if start_date < fy.start_date or end_date > fy.end_date:
                raise serializers.ValidationError("Period dates must be within fiscal year boundaries.")

        return attrs


class AccountTypeSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    normal_balance_display = serializers.CharField(source="get_normal_balance_display", read_only=True)
    accounts_count = serializers.SerializerMethodField()

    class Meta:
        model = AccountType
        fields = [
            "id",
            "name",
            "category",
            "category_display",
            "normal_balance",
            "normal_balance_display",
            "code_prefix",
            "description",
            "is_system",
            "accounts_count",
        ]
        read_only_fields = ["id", "category_display", "normal_balance_display", "accounts_count"]

    def get_accounts_count(self, obj):
        # Count accounts for the requesting user's company
        request = self.context.get("request")
        if request and hasattr(request.user, "company"):
            return obj.accounts.filter(company=request.user.company).count()
        return obj.accounts.count()


class AccountSerializer(serializers.ModelSerializer):
    account_type_name = serializers.ReadOnlyField(source="account_type.name")
    account_type_category = serializers.ReadOnlyField(source="account_type.category")
    account_type_normal_balance = serializers.ReadOnlyField(source="account_type.normal_balance")
    parent_code = serializers.ReadOnlyField(source="parent.code")
    parent_name = serializers.ReadOnlyField(source="parent.name")
    is_header = serializers.BooleanField(read_only=True)

    class Meta:
        model = Account
        fields = [
            "id",
            "code",
            "name",
            "account_type",
            "account_type_name",
            "account_type_category",
            "account_type_normal_balance",
            "parent",
            "parent_code",
            "parent_name",
            "description",
            "currency",
            "is_active",
            "is_header",
            "is_reconciled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "account_type_name",
            "account_type_category",
            "account_type_normal_balance",
            "parent_code",
            "parent_name",
            "is_header",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        parent = attrs.get("parent")
        if parent:
            if self.instance and parent.id == self.instance.id:
                raise serializers.ValidationError({"parent": "An account cannot be its own parent."})

            # Check circular reference
            curr = parent
            visited = {self.instance.id} if self.instance else set()
            while curr is not None:
                if curr.id in visited:
                    raise serializers.ValidationError({"parent": "Circular parent account hierarchy detected."})
                visited.add(curr.id)
                curr = curr.parent

        return attrs


class AccountTreeSerializer(serializers.ModelSerializer):
    account_type_name = serializers.ReadOnlyField(source="account_type.name")
    account_type_category = serializers.ReadOnlyField(source="account_type.category")
    account_type_normal_balance = serializers.ReadOnlyField(source="account_type.normal_balance")
    children = serializers.SerializerMethodField()

    class Meta:
        model = Account
        fields = [
            "id",
            "code",
            "name",
            "account_type",
            "account_type_name",
            "account_type_category",
            "account_type_normal_balance",
            "currency",
            "is_active",
            "description",
            "children",
        ]

    def get_children(self, obj):
        children = obj.children.filter(is_active=True).order_by("code")
        return AccountTreeSerializer(children, many=True, context=self.context).data


class AccountingSettingsSerializer(serializers.ModelSerializer):
    current_fiscal_year_name = serializers.ReadOnlyField(source="current_fiscal_year.name")
    retained_earnings_account_code = serializers.ReadOnlyField(source="retained_earnings_account.code")
    retained_earnings_account_name = serializers.ReadOnlyField(source="retained_earnings_account.name")
    inventory_raw_material_account_code = serializers.ReadOnlyField(source="inventory_raw_material_account.code")
    inventory_raw_material_account_name = serializers.ReadOnlyField(source="inventory_raw_material_account.name")
    inventory_finished_goods_account_code = serializers.ReadOnlyField(source="inventory_finished_goods_account.code")
    inventory_finished_goods_account_name = serializers.ReadOnlyField(source="inventory_finished_goods_account.name")
    inventory_clearing_account_code = serializers.ReadOnlyField(source="inventory_clearing_account.code")
    inventory_clearing_account_name = serializers.ReadOnlyField(source="inventory_clearing_account.name")
    inventory_cogs_account_code = serializers.ReadOnlyField(source="inventory_cogs_account.code")
    inventory_cogs_account_name = serializers.ReadOnlyField(source="inventory_cogs_account.name")
    inventory_adjustment_account_code = serializers.ReadOnlyField(source="inventory_adjustment_account.code")
    inventory_adjustment_account_name = serializers.ReadOnlyField(source="inventory_adjustment_account.name")
    inventory_write_off_account_code = serializers.ReadOnlyField(source="inventory_write_off_account.code")
    inventory_write_off_account_name = serializers.ReadOnlyField(source="inventory_write_off_account.name")
    manufacturing_wip_account_code = serializers.ReadOnlyField(source="manufacturing_wip_account.code")
    manufacturing_wip_account_name = serializers.ReadOnlyField(source="manufacturing_wip_account.name")
    manufacturing_labor_account_code = serializers.ReadOnlyField(source="manufacturing_labor_account.code")
    manufacturing_labor_account_name = serializers.ReadOnlyField(source="manufacturing_labor_account.name")
    manufacturing_overhead_account_code = serializers.ReadOnlyField(source="manufacturing_overhead_account.code")
    manufacturing_overhead_account_name = serializers.ReadOnlyField(source="manufacturing_overhead_account.name")
    manufacturing_scrap_account_code = serializers.ReadOnlyField(source="manufacturing_scrap_account.code")
    manufacturing_scrap_account_name = serializers.ReadOnlyField(source="manufacturing_scrap_account.name")
    manufacturing_variance_account_code = serializers.ReadOnlyField(source="manufacturing_variance_account.code")
    manufacturing_variance_account_name = serializers.ReadOnlyField(source="manufacturing_variance_account.name")
    expenses_default_cash_account_code = serializers.ReadOnlyField(source="expenses_default_cash_account.code")
    expenses_default_cash_account_name = serializers.ReadOnlyField(source="expenses_default_cash_account.name")
    expenses_default_bank_account_code = serializers.ReadOnlyField(source="expenses_default_bank_account.code")
    expenses_default_bank_account_name = serializers.ReadOnlyField(source="expenses_default_bank_account.name")
    expenses_default_payable_account_code = serializers.ReadOnlyField(source="expenses_default_payable_account.code")
    expenses_default_payable_account_name = serializers.ReadOnlyField(source="expenses_default_payable_account.name")
    expenses_default_employee_payable_account_code = serializers.ReadOnlyField(source="expenses_default_employee_payable_account.code")
    expenses_default_employee_payable_account_name = serializers.ReadOnlyField(source="expenses_default_employee_payable_account.name")
    expenses_default_tax_account_code = serializers.ReadOnlyField(source="expenses_default_tax_account.code")
    expenses_default_tax_account_name = serializers.ReadOnlyField(source="expenses_default_tax_account.name")

    class Meta:
        model = AccountingSettings
        fields = [
            "id",
            "default_currency",
            "current_fiscal_year",
            "current_fiscal_year_name",
            "retained_earnings_account",
            "retained_earnings_account_code",
            "retained_earnings_account_name",
            "lock_date",
            "allow_direct_posting_to_parent_accounts",
            "inventory_accounting_enabled",
            "inventory_costing_method",
            "inventory_raw_material_account",
            "inventory_raw_material_account_code",
            "inventory_raw_material_account_name",
            "inventory_finished_goods_account",
            "inventory_finished_goods_account_code",
            "inventory_finished_goods_account_name",
            "inventory_clearing_account",
            "inventory_clearing_account_code",
            "inventory_clearing_account_name",
            "inventory_cogs_account",
            "inventory_cogs_account_code",
            "inventory_cogs_account_name",
            "inventory_adjustment_account",
            "inventory_adjustment_account_code",
            "inventory_adjustment_account_name",
            "inventory_write_off_account",
            "inventory_write_off_account_code",
            "inventory_write_off_account_name",
            "manufacturing_accounting_enabled",
            "wip_accounting_enabled",
            "labor_accounting_enabled",
            "overhead_accounting_enabled",
            "scrap_accounting_enabled",
            "variance_accounting_enabled",
            "labor_rate_per_unit",
            "overhead_rate_per_unit",
            "manufacturing_wip_account",
            "manufacturing_wip_account_code",
            "manufacturing_wip_account_name",
            "manufacturing_labor_account",
            "manufacturing_labor_account_code",
            "manufacturing_labor_account_name",
            "manufacturing_overhead_account",
            "manufacturing_overhead_account_code",
            "manufacturing_overhead_account_name",
            "manufacturing_scrap_account",
            "manufacturing_scrap_account_code",
            "manufacturing_scrap_account_name",
            "manufacturing_variance_account",
            "manufacturing_variance_account_code",
            "manufacturing_variance_account_name",
            "expenses_accounting_enabled",
            "expenses_require_approval",
            "expenses_default_cash_account",
            "expenses_default_cash_account_code",
            "expenses_default_cash_account_name",
            "expenses_default_bank_account",
            "expenses_default_bank_account_code",
            "expenses_default_bank_account_name",
            "expenses_default_payable_account",
            "expenses_default_payable_account_code",
            "expenses_default_payable_account_name",
            "expenses_default_employee_payable_account",
            "expenses_default_employee_payable_account_code",
            "expenses_default_employee_payable_account_name",
            "expenses_default_tax_account",
            "expenses_default_tax_account_code",
            "expenses_default_tax_account_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "current_fiscal_year_name",
            "retained_earnings_account_code",
            "retained_earnings_account_name",
            "inventory_raw_material_account_code",
            "inventory_raw_material_account_name",
            "inventory_finished_goods_account_code",
            "inventory_finished_goods_account_name",
            "inventory_clearing_account_code",
            "inventory_clearing_account_name",
            "inventory_cogs_account_code",
            "inventory_cogs_account_name",
            "inventory_adjustment_account_code",
            "inventory_adjustment_account_name",
            "inventory_write_off_account_code",
            "inventory_write_off_account_name",
            "manufacturing_wip_account_code",
            "manufacturing_wip_account_name",
            "manufacturing_labor_account_code",
            "manufacturing_labor_account_name",
            "manufacturing_overhead_account_code",
            "manufacturing_overhead_account_name",
            "manufacturing_scrap_account_code",
            "manufacturing_scrap_account_name",
            "manufacturing_variance_account_code",
            "manufacturing_variance_account_name",
            "expenses_default_cash_account_code",
            "expenses_default_cash_account_name",
            "expenses_default_bank_account_code",
            "expenses_default_bank_account_name",
            "expenses_default_payable_account_code",
            "expenses_default_payable_account_name",
            "expenses_default_employee_payable_account_code",
            "expenses_default_employee_payable_account_name",
            "expenses_default_tax_account_code",
            "expenses_default_tax_account_name",
            "created_at",
            "updated_at",
        ]


from .models import JournalEntry, JournalEntryLine
from decimal import Decimal


class JournalEntryLineSerializer(serializers.ModelSerializer):
    account_code = serializers.ReadOnlyField(source="account.code")
    account_name = serializers.ReadOnlyField(source="account.name")
    account_category = serializers.ReadOnlyField(source="account.account_type.category")

    class Meta:
        model = JournalEntryLine
        fields = [
            "id",
            "account",
            "account_code",
            "account_name",
            "account_category",
            "line_number",
            "debit",
            "credit",
            "description",
            "created_at",
        ]
        read_only_fields = ["id", "account_code", "account_name", "account_category", "created_at"]

    def validate(self, attrs):
        debit = attrs.get("debit", Decimal("0.00"))
        credit = attrs.get("credit", Decimal("0.00"))
        if debit < 0 or credit < 0:
            raise serializers.ValidationError("Debit and credit amounts cannot be negative.")
        if debit == 0 and credit == 0:
            raise serializers.ValidationError("A line must have either a non-zero debit or credit amount.")
        if debit > 0 and credit > 0:
            raise serializers.ValidationError("A line cannot contain both a debit and credit amount. Use separate lines.")
        return attrs


class JournalEntrySerializer(serializers.ModelSerializer):
    lines = JournalEntryLineSerializer(many=True, required=False)
    accounting_period_name = serializers.ReadOnlyField(source="accounting_period.name")
    posted_by_name = serializers.ReadOnlyField(source="posted_by.username")
    created_by_name = serializers.ReadOnlyField(source="created_by.username")
    reversal_of_entry_number = serializers.ReadOnlyField(source="reversal_of.entry_number")
    total_debit = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    total_credit = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    is_balanced = serializers.BooleanField(read_only=True)

    class Meta:
        model = JournalEntry
        fields = [
            "id",
            "entry_number",
            "transaction_date",
            "accounting_period",
            "accounting_period_name",
            "reference",
            "description",
            "source_module",
            "source_id",
            "status",
            "posted_at",
            "posted_by",
            "posted_by_name",
            "reversal_of",
            "reversal_of_entry_number",
            "created_by",
            "created_by_name",
            "total_debit",
            "total_credit",
            "is_balanced",
            "lines",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "entry_number",
            "accounting_period_name",
            "status",
            "posted_at",
            "posted_by",
            "posted_by_name",
            "reversal_of",
            "reversal_of_entry_number",
            "created_by",
            "created_by_name",
            "total_debit",
            "total_credit",
            "is_balanced",
            "created_at",
            "updated_at",
        ]

    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        request = self.context.get("request")
        company = request.user.company if request and hasattr(request.user, "company") else None
        user = request.user if request and request.user.is_authenticated else None

        if not company:
            raise serializers.ValidationError("Company context is required to create a journal entry.")

        validated_data["company"] = company
        validated_data["created_by"] = user
        validated_data["status"] = "draft"

        try:
            entry = JournalEntry.objects.create(**validated_data)

            for idx, line_data in enumerate(lines_data, start=1):
                line_data["company"] = company
                line_data["journal_entry"] = entry
                line_data["line_number"] = line_data.get("line_number") or idx
                JournalEntryLine.objects.create(**line_data)

            return entry
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            raise serializers.ValidationError(msg)

    def update(self, instance, validated_data):
        if instance.status != "draft":
            raise serializers.ValidationError("Cannot modify a journal entry that is not in draft status.")

        lines_data = validated_data.pop("lines", None)

        try:
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            if lines_data is not None:
                # Replace lines for draft entry
                instance.lines.all().delete()
                company = instance.company
                for idx, line_data in enumerate(lines_data, start=1):
                    line_data["company"] = company
                    line_data["journal_entry"] = instance
                    line_data["line_number"] = line_data.get("line_number") or idx
                    JournalEntryLine.objects.create(**line_data)

            return instance
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            raise serializers.ValidationError(msg)


# ==============================================================================
# BLUEPRINT SECTION #16 — EXPENSES SERIALIZERS
# ==============================================================================

from .models import Expense, ExpenseCategory, ExpenseAuditLog


class ExpenseCategorySerializer(serializers.ModelSerializer):
    expense_account_code = serializers.ReadOnlyField(source="expense_account.code")
    expense_account_name = serializers.ReadOnlyField(source="expense_account.name")
    expenses_count = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseCategory
        fields = [
            "id",
            "code",
            "name",
            "description",
            "expense_account",
            "expense_account_code",
            "expense_account_name",
            "is_active",
            "expenses_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "expense_account_code",
            "expense_account_name",
            "expenses_count",
            "created_at",
            "updated_at",
        ]

    def get_expenses_count(self, obj):
        return obj.expenses.count()


class ExpenseAuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseAuditLog
        fields = [
            "id",
            "action",
            "actor",
            "actor_name",
            "details",
            "notes",
            "created_at",
        ]
        read_only_fields = ["id", "actor_name", "created_at"]

    def get_actor_name(self, obj):
        if obj.actor:
            return obj.actor.get_full_name() or obj.actor.username
        return None


class ExpenseSerializer(serializers.ModelSerializer):
    category_name = serializers.ReadOnlyField(source="category.name")
    category_code = serializers.ReadOnlyField(source="category.code")
    employee_name = serializers.SerializerMethodField()
    vendor_name = serializers.SerializerMethodField()
    expense_account_code = serializers.ReadOnlyField(source="expense_account.code")
    expense_account_name = serializers.ReadOnlyField(source="expense_account.name")
    tax_account_code = serializers.ReadOnlyField(source="tax_account.code")
    tax_account_name = serializers.ReadOnlyField(source="tax_account.name")
    payment_account_code = serializers.ReadOnlyField(source="payment_account.code")
    payment_account_name = serializers.ReadOnlyField(source="payment_account.name")
    journal_entry_number = serializers.ReadOnlyField(source="journal_entry.entry_number")
    reversal_journal_entry_number = serializers.ReadOnlyField(source="reversal_journal_entry.entry_number")
    created_by_name = serializers.SerializerMethodField()
    submitted_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    posted_by_name = serializers.SerializerMethodField()
    receipt_url = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = [
            "id",
            "expense_number",
            "expense_type",
            "employee",
            "employee_name",
            "vendor",
            "vendor_name",
            "vendor_name_raw",
            "category",
            "category_name",
            "category_code",
            "title",
            "description",
            "expense_date",
            "accounting_date",
            "amount_before_tax",
            "tax_amount",
            "total_amount",
            "currency",
            "payment_source",
            "expense_account",
            "expense_account_code",
            "expense_account_name",
            "tax_account",
            "tax_account_code",
            "tax_account_name",
            "payment_account",
            "payment_account_code",
            "payment_account_name",
            "approval_status",
            "accounting_status",
            "receipt",
            "receipt_name",
            "receipt_size",
            "receipt_content_type",
            "receipt_url",
            "notes",
            "rejection_reason",
            "journal_entry",
            "journal_entry_number",
            "reversal_journal_entry",
            "reversal_journal_entry_number",
            "created_by",
            "created_by_name",
            "submitted_by",
            "submitted_by_name",
            "submitted_at",
            "approved_by",
            "approved_by_name",
            "approved_at",
            "posted_by",
            "posted_by_name",
            "posted_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "expense_number",
            "total_amount",
            "category_name",
            "category_code",
            "employee_name",
            "vendor_name",
            "expense_account_code",
            "expense_account_name",
            "tax_account_code",
            "tax_account_name",
            "payment_account_code",
            "payment_account_name",
            "journal_entry_number",
            "reversal_journal_entry_number",
            "created_by_name",
            "submitted_by_name",
            "submitted_at",
            "approved_by_name",
            "approved_at",
            "posted_by_name",
            "posted_at",
            "receipt_url",
            "created_at",
            "updated_at",
        ]

    def get_employee_name(self, obj):
        if not obj.employee:
            return None
        return f"{obj.employee.first_name} {obj.employee.last_name}".strip() or str(obj.employee)

    def get_vendor_name(self, obj):
        if obj.vendor:
            return obj.vendor.name
        return obj.vendor_name_raw or None

    def get_created_by_name(self, obj):
        return (obj.created_by.get_full_name() or obj.created_by.username) if obj.created_by else None

    def get_submitted_by_name(self, obj):
        return (obj.submitted_by.get_full_name() or obj.submitted_by.username) if obj.submitted_by else None

    def get_approved_by_name(self, obj):
        return (obj.approved_by.get_full_name() or obj.approved_by.username) if obj.approved_by else None

    def get_posted_by_name(self, obj):
        return (obj.posted_by.get_full_name() or obj.posted_by.username) if obj.posted_by else None

    def get_receipt_url(self, obj):
        if obj.receipt:
            try:
                return obj.receipt.url
            except Exception:
                return None
        return None


class ExpenseDetailSerializer(ExpenseSerializer):
    audit_logs = ExpenseAuditLogSerializer(many=True, read_only=True)
    journal_lines = serializers.SerializerMethodField()

    class Meta(ExpenseSerializer.Meta):
        fields = ExpenseSerializer.Meta.fields + ["audit_logs", "journal_lines"]

    def get_journal_lines(self, obj):
        if obj.journal_entry:
            return [
                {
                    "line_number": line.line_number,
                    "account_code": line.account.code,
                    "account_name": line.account.name,
                    "category": line.account.account_type.category,
                    "debit": float(line.debit),
                    "credit": float(line.credit),
                    "description": line.description,
                }
                for line in obj.journal_entry.lines.select_related("account", "account__account_type").all()
            ]
        return []


class ExpenseCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = [
            "id",
            "expense_number",
            "expense_type",
            "employee",
            "vendor",
            "vendor_name_raw",
            "category",
            "title",
            "description",
            "expense_date",
            "amount_before_tax",
            "tax_amount",
            "total_amount",
            "currency",
            "payment_source",
            "expense_account",
            "tax_account",
            "payment_account",
            "approval_status",
            "accounting_status",
            "receipt",
            "notes",
        ]
        read_only_fields = ["id", "expense_number", "total_amount", "approval_status", "accounting_status"]

    def validate(self, attrs):
        amount_before_tax = attrs.get("amount_before_tax") or getattr(self.instance, "amount_before_tax", Decimal("0.00"))
        tax_amount = attrs.get("tax_amount") or getattr(self.instance, "tax_amount", Decimal("0.00"))

        if amount_before_tax < Decimal("0.00"):
            raise serializers.ValidationError({"amount_before_tax": "Amount before tax cannot be negative."})
        if tax_amount < Decimal("0.00"):
            raise serializers.ValidationError({"tax_amount": "Tax amount cannot be negative."})

        return attrs


# ==============================================================================
# BLUEPRINT SECTION #17 — CASH & BANK SERIALIZERS
# ==============================================================================

class BankAccountSerializer(serializers.ModelSerializer):
    gl_account_code = serializers.ReadOnlyField(source="gl_account.code")
    gl_account_name = serializers.ReadOnlyField(source="gl_account.name")
    masked_account_number = serializers.ReadOnlyField()
    current_gl_balance = serializers.SerializerMethodField()
    unreconciled_difference = serializers.SerializerMethodField()
    opening_balance_journal_entry_number = serializers.ReadOnlyField(source="opening_balance_journal_entry.entry_number")

    class Meta:
        model = BankAccount
        fields = [
            "id",
            "account_name",
            "bank_name",
            "masked_account_number",
            "routing_number",
            "swift_bic",
            "account_type",
            "currency",
            "gl_account",
            "gl_account_code",
            "gl_account_name",
            "opening_balance",
            "opening_balance_date",
            "opening_balance_posted",
            "opening_balance_journal_entry",
            "opening_balance_journal_entry_number",
            "reconciled_balance",
            "current_gl_balance",
            "unreconciled_difference",
            "last_reconciliation_date",
            "is_active",
            "description",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "masked_account_number",
            "gl_account_code",
            "gl_account_name",
            "opening_balance_posted",
            "opening_balance_journal_entry",
            "opening_balance_journal_entry_number",
            "reconciled_balance",
            "current_gl_balance",
            "unreconciled_difference",
            "last_reconciliation_date",
            "created_at",
            "updated_at",
        ]

    def get_current_gl_balance(self, obj):
        return float(obj.current_gl_balance)

    def get_unreconciled_difference(self, obj):
        return float(obj.unreconciled_difference)


class BankAccountCreateUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = [
            "id",
            "account_name",
            "bank_name",
            "account_number",
            "routing_number",
            "swift_bic",
            "account_type",
            "currency",
            "gl_account",
            "is_active",
            "description",
        ]
        read_only_fields = ["id"]
        extra_kwargs = {
            "account_number": {"write_only": True},
        }

    def validate_gl_account(self, value):
        if value:
            if value.is_header:
                raise serializers.ValidationError("GL Account must be a leaf account, not a header.")
            if not value.is_active:
                raise serializers.ValidationError("GL Account must be active.")
        return value


class BankTransactionSerializer(serializers.ModelSerializer):
    bank_account_name = serializers.ReadOnlyField(source="bank_account.account_name")
    bank_name = serializers.ReadOnlyField(source="bank_account.bank_name")
    journal_entry_number = serializers.ReadOnlyField(source="journal_entry.entry_number")
    reconciliation_number = serializers.ReadOnlyField(source="reconciliation.reconciliation_number")
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BankTransaction
        fields = [
            "id",
            "bank_account",
            "bank_account_name",
            "bank_name",
            "transaction_date",
            "value_date",
            "amount",
            "direction",
            "transaction_type",
            "source",
            "description",
            "reference",
            "counterparty",
            "external_id",
            "matching_status",
            "reconciliation_status",
            "reconciliation",
            "reconciliation_number",
            "journal_entry",
            "journal_entry_number",
            "journal_entry_line",
            "source_document_ref",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "bank_account_name",
            "bank_name",
            "journal_entry_number",
            "reconciliation_number",
            "created_by_name",
            "created_at",
            "updated_at",
        ]

    def get_created_by_name(self, obj):
        return (obj.created_by.get_full_name() or obj.created_by.username) if obj.created_by else None


class BankReconciliationSerializer(serializers.ModelSerializer):
    bank_account_name = serializers.ReadOnlyField(source="bank_account.account_name")
    bank_name = serializers.ReadOnlyField(source="bank_account.bank_name")
    reconciled_by_name = serializers.SerializerMethodField()
    transactions_count = serializers.SerializerMethodField()

    class Meta:
        model = BankReconciliation
        fields = [
            "id",
            "reconciliation_number",
            "bank_account",
            "bank_account_name",
            "bank_name",
            "statement_date",
            "statement_balance",
            "starting_balance",
            "reconciled_balance",
            "difference",
            "status",
            "notes",
            "reconciled_by",
            "reconciled_by_name",
            "reconciled_at",
            "transactions_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "reconciliation_number",
            "bank_account_name",
            "bank_name",
            "reconciled_by_name",
            "transactions_count",
            "created_at",
            "updated_at",
        ]

    def get_reconciled_by_name(self, obj):
        return (obj.reconciled_by.get_full_name() or obj.reconciled_by.username) if obj.reconciled_by else None

    def get_transactions_count(self, obj):
        return obj.transactions.count()


class BankAuditLogSerializer(serializers.ModelSerializer):
    bank_account_name = serializers.ReadOnlyField(source="bank_account.account_name")
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = BankAuditLog
        fields = [
            "id",
            "bank_account",
            "bank_account_name",
            "reconciliation",
            "action",
            "actor",
            "actor_name",
            "details",
            "notes",
            "created_at",
        ]
        read_only_fields = ["id", "bank_account_name", "actor_name", "created_at"]

    def get_actor_name(self, obj):
        return (obj.actor.get_full_name() or obj.actor.username) if obj.actor else None


# Action Payload Input Serializers

class OpeningBalanceInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    date = serializers.DateField()
    equity_account = serializers.IntegerField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class DepositInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0.01"))
    date = serializers.DateField()
    offset_account = serializers.IntegerField()
    description = serializers.CharField(required=False, allow_blank=True, default="")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    counterparty = serializers.CharField(required=False, allow_blank=True, default="")


class WithdrawalInputSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0.01"))
    date = serializers.DateField()
    offset_account = serializers.IntegerField()
    description = serializers.CharField(required=False, allow_blank=True, default="")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    counterparty = serializers.CharField(required=False, allow_blank=True, default="")
    transaction_type = serializers.ChoiceField(
        choices=["withdrawal", "fee", "other"],
        default="withdrawal"
    )


class BankTransferInputSerializer(serializers.Serializer):
    from_account = serializers.IntegerField()
    to_account = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0.01"))
    date = serializers.DateField()
    description = serializers.CharField(required=False, allow_blank=True, default="")
    reference = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["from_account"] == attrs["to_account"]:
            raise serializers.ValidationError("Source and destination accounts must be different.")
        return attrs


class ReconciliationInputSerializer(serializers.Serializer):
    statement_date = serializers.DateField()
    statement_balance = serializers.DecimalField(max_digits=18, decimal_places=2)
    transaction_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    journal_line_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, default="")




