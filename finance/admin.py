from django.contrib import admin
from .models import DepartmentBudget, ExpenseRequest, OperationalCost, PayrollRecord, FinancialSummary


@admin.register(DepartmentBudget)
class DepartmentBudgetAdmin(admin.ModelAdmin):
    list_display = ["department", "period_label", "total_budget", "auto_approve_limit", "is_active"]
    list_filter = ["department", "period", "is_active"]


@admin.register(ExpenseRequest)
class ExpenseRequestAdmin(admin.ModelAdmin):
    list_display = ["title", "amount", "status", "requested_by", "created_at"]
    list_filter = ["status", "category"]


@admin.register(OperationalCost)
class OperationalCostAdmin(admin.ModelAdmin):
    list_display = ["title", "department", "cost_type", "amount", "date"]
    list_filter = ["department", "cost_type"]


@admin.register(PayrollRecord)
class PayrollRecordAdmin(admin.ModelAdmin):
    list_display = ["employee", "period_label", "net_salary", "pay_status"]
    list_filter = ["pay_status", "period_label"]


@admin.register(FinancialSummary)
class FinancialSummaryAdmin(admin.ModelAdmin):
    list_display = ["period_label", "total_revenue", "net_profit"]
