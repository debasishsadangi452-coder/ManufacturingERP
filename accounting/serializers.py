from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from .models import FiscalYear, AccountingPeriod, AccountType, Account, AccountingSettings


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


