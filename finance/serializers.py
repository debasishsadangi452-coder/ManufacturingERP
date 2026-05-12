from rest_framework import serializers
from .models import DepartmentBudget, ExpenseRequest, OperationalCost, PayrollRecord, FinancialSummary
from django.contrib.auth import get_user_model

User = get_user_model()


class UserMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "role", "email"]


class DepartmentBudgetSerializer(serializers.ModelSerializer):
    set_by_info = UserMiniSerializer(source="set_by", read_only=True)
    spent = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()
    utilization_pct = serializers.SerializerMethodField()

    class Meta:
        model = DepartmentBudget
        fields = [
            "id", "department", "period", "period_label",
            "total_budget", "auto_approve_limit",
            "set_by", "set_by_info", "is_active",
            "spent", "remaining", "utilization_pct",
            "created_at", "updated_at",
        ]
        read_only_fields = ["set_by", "created_at", "updated_at"]

    def get_spent(self, obj):
        return float(obj.spent)

    def get_remaining(self, obj):
        return float(obj.remaining)

    def get_utilization_pct(self, obj):
        return round(obj.utilization_pct, 1)


class ExpenseRequestSerializer(serializers.ModelSerializer):
    requested_by_info = UserMiniSerializer(source="requested_by", read_only=True)
    reviewed_by_info = UserMiniSerializer(source="reviewed_by", read_only=True)
    budget_info = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseRequest
        fields = [
            "id", "title", "description", "category", "amount",
            "budget", "budget_info",
            "requested_by", "requested_by_info",
            "vendor", "reference_number",
            "status", "notes",
            "reviewed_by", "reviewed_by_info", "reviewed_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "requested_by", "status", "reviewed_by", "reviewed_at",
            "created_at", "updated_at",
        ]

    def get_budget_info(self, obj):
        if obj.budget:
            return {
                "id": obj.budget.id,
                "department": obj.budget.department,
                "department_display": obj.budget.get_department_display(),
                "period_label": obj.budget.period_label,
                "auto_approve_limit": float(obj.budget.auto_approve_limit),
                "remaining": float(obj.budget.remaining),
            }
        return None


class ExpenseApprovalSerializer(serializers.Serializer):
    """Used by admin to approve/reject expense requests."""
    status = serializers.ChoiceField(choices=["approved", "rejected"])
    notes = serializers.CharField(required=False, allow_blank=True)


class OperationalCostSerializer(serializers.ModelSerializer):
    recorded_by_info = UserMiniSerializer(source="recorded_by", read_only=True)

    class Meta:
        model = OperationalCost
        fields = [
            "id", "title", "cost_type", "department", "amount",
            "date", "vendor", "invoice_number",
            "expense_request", "recorded_by", "recorded_by_info",
            "notes", "created_at",
        ]
        read_only_fields = ["recorded_by", "created_at"]


class PayrollRecordSerializer(serializers.ModelSerializer):
    employee_info = UserMiniSerializer(source="employee", read_only=True)
    processed_by_info = UserMiniSerializer(source="processed_by", read_only=True)

    class Meta:
        model = PayrollRecord
        fields = [
            "id", "employee", "employee_info", "period_label",
            "basic_salary", "allowances", "overtime_pay",
            "deductions", "tax", "net_salary",
            "pay_status", "processed_by", "processed_by_info",
            "payment_date", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["net_salary", "created_at", "updated_at"]


class FinancialSummarySerializer(serializers.ModelSerializer):
    created_by_info = UserMiniSerializer(source="created_by", read_only=True)
    profit_margin = serializers.SerializerMethodField()

    class Meta:
        model = FinancialSummary
        fields = [
            "id", "period_label",
            "total_revenue", "total_expenses", "total_payroll", "cogs", "net_profit",
            "profit_margin",
            "notes", "created_by", "created_by_info",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def get_profit_margin(self, obj):
        if obj.total_revenue and float(obj.total_revenue) > 0:
            return round(float(obj.net_profit) / float(obj.total_revenue) * 100, 2)
        return 0
