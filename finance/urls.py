from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    DepartmentBudgetViewSet,
    ExpenseRequestViewSet,
    OperationalCostViewSet,
    PayrollRecordViewSet,
    FinancialSummaryViewSet,
)

router = DefaultRouter()
router.register(r"budgets", DepartmentBudgetViewSet, basename="budget")
router.register(r"expenses", ExpenseRequestViewSet, basename="expense")
router.register(r"operational-costs", OperationalCostViewSet, basename="operational-cost")
router.register(r"payroll", PayrollRecordViewSet, basename="payroll")
router.register(r"summaries", FinancialSummaryViewSet, basename="financial-summary")

urlpatterns = [
    path("", include(router.urls)),
]
