from rest_framework import serializers
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
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "current_fiscal_year_name",
            "retained_earnings_account_code",
            "retained_earnings_account_name",
            "created_at",
            "updated_at",
        ]
