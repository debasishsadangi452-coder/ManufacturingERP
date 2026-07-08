from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from .models import DepartmentBudget, ExpenseRequest, OperationalCost, PayrollRecord, FinancialSummary
from .serializers import (
    DepartmentBudgetSerializer, ExpenseRequestSerializer,
    ExpenseApprovalSerializer, OperationalCostSerializer,
    PayrollRecordSerializer, FinancialSummarySerializer,
)
from accounts.permission import IsAdmin
from django.contrib.auth import get_user_model
from core.tenancy import CompanyScopedMixin

User = get_user_model()


class IsFinanceOrAdmin(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role in ["admin", "finance"]


# ────────────────────────────────────────────────────────────────────────────
# Department Budget  – Admin CRUD, all authenticated users can read
# ────────────────────────────────────────────────────────────────────────────
class DepartmentBudgetViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = DepartmentBudget.objects.all()
    serializer_class = DepartmentBudgetSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["department", "period", "is_active"]

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsFinanceOrAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(set_by=self.request.user, company=self.request.user.company)


# ────────────────────────────────────────────────────────────────────────────
# Expense Request  – any authenticated user can submit;
#                    Admin can approve/reject
# ────────────────────────────────────────────────────────────────────────────
class ExpenseRequestViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = ExpenseRequest.objects.select_related(
        "budget", "requested_by", "reviewed_by"
    ).all()
    serializer_class = ExpenseRequestSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "category", "budget__department"]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if user.role in ["admin", "finance"]:
            return qs
        # Non-admins only see their own requests
        return qs.filter(requested_by=user)

    def perform_create(self, serializer):
        serializer.save(requested_by=self.request.user, company=self.request.user.company)

    @action(detail=True, methods=["post"], permission_classes=[IsFinanceOrAdmin])
    def approve(self, request, pk=None):
        """Admin approves an expense request."""
        expense = self.get_object()
        ser = ExpenseApprovalSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        new_status = ser.validated_data["status"]
        notes = ser.validated_data.get("notes", "")

        if expense.status not in ["pending"]:
            return Response(
                {"detail": f"Cannot change status from '{expense.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        expense.status = new_status
        expense.notes = notes
        expense.reviewed_by = request.user
        expense.reviewed_at = timezone.now()
        expense.save()
        return Response(ExpenseRequestSerializer(expense).data)

    @action(detail=False, methods=["get"], permission_classes=[IsFinanceOrAdmin])
    def pending_approvals(self, request):
        """Return all pending expense requests for admin dashboard."""
        pending = ExpenseRequest.objects.filter(status="pending").select_related(
            "budget", "requested_by"
        )
        return Response(ExpenseRequestSerializer(pending, many=True).data)

    @action(detail=False, methods=["get"])
    def my_requests(self, request):
        qs = ExpenseRequest.objects.filter(requested_by=request.user)
        return Response(ExpenseRequestSerializer(qs, many=True).data)


# ────────────────────────────────────────────────────────────────────────────
# Operational Costs  – Admin CRUD; read for authenticated
# ────────────────────────────────────────────────────────────────────────────
class OperationalCostViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = OperationalCost.objects.select_related("recorded_by").all()
    serializer_class = OperationalCostSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["department", "cost_type"]

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsFinanceOrAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user, company=self.request.user.company)


# ────────────────────────────────────────────────────────────────────────────
# Payroll  – Admin CRUD only; employees can read their own records
# ────────────────────────────────────────────────────────────────────────────
class PayrollRecordViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "employee__company"
    queryset = PayrollRecord.objects.select_related("employee", "processed_by").all()
    serializer_class = PayrollRecordSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["pay_status", "period_label", "employee"]

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy", "process_payroll"]:
            return [IsFinanceOrAdmin()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if user.role in ["admin", "finance"]:
            return qs
        return qs.filter(employee=user)

    @action(detail=True, methods=["post"], permission_classes=[IsFinanceOrAdmin])
    def process_payroll(self, request, pk=None):
        """Mark payroll as processed."""
        record = self.get_object()
        record.pay_status = "processed"
        record.processed_by = request.user
        record.save()
        return Response(PayrollRecordSerializer(record).data)

    @action(detail=True, methods=["post"], permission_classes=[IsFinanceOrAdmin])
    def mark_paid(self, request, pk=None):
        """Mark payroll as paid."""
        import datetime
        record = self.get_object()
        record.pay_status = "paid"
        record.payment_date = datetime.date.today()
        record.save()
        return Response(PayrollRecordSerializer(record).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Summary of payroll totals by period."""
        from django.db.models import Sum, Count
        qs = self.get_queryset()
        period = request.query_params.get("period_label")
        if period:
            qs = qs.filter(period_label=period)
        data = qs.aggregate(
            total_basic=Sum("basic_salary"),
            total_net=Sum("net_salary"),
            total_tax=Sum("tax"),
            total_deductions=Sum("deductions"),
            count=Count("id"),
        )
        return Response(data)


# ────────────────────────────────────────────────────────────────────────────
# Financial Summary  – Admin CRUD; all can read
# ────────────────────────────────────────────────────────────────────────────
class FinancialSummaryViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = FinancialSummary.objects.all()
    serializer_class = FinancialSummarySerializer

    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsFinanceOrAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, company=self.request.user.company)

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        """Return aggregated KPI data for the finance dashboard."""
        from django.db.models import Sum
        from decimal import Decimal

        budgets = DepartmentBudget.objects.filter(is_active=True, company=request.user.company)
        total_budget = float(sum(b.total_budget for b in budgets))
        total_spent = float(sum(b.spent for b in budgets))

        pending_count = ExpenseRequest.objects.filter(status="pending", company=request.user.company).count()
        total_costs = float(
            OperationalCost.objects.filter(company=request.user.company).aggregate(t=Sum("amount"))["t"] or 0
        )
        payroll_total = float(
            PayrollRecord.objects.filter(pay_status__in=["processed", "paid"], employee__company=request.user.company)
            .aggregate(t=Sum("net_salary"))["t"] or 0
        )

        # Revenue recognized on fulfilment: shipped or delivered sales orders
        from sales.models import SalesOrder
        total_revenue = float(
            SalesOrder.objects.filter(
                status__in=["shipped", "delivered"],
                customer__company=request.user.company,
            ).aggregate(t=Sum("total_amount"))["t"] or 0
        )

        return Response({
            "total_revenue": total_revenue,
            "total_budget": total_budget,
            "total_spent": total_spent,
            "budget_remaining": total_budget - total_spent,
            "pending_approvals": pending_count,
            "total_operational_costs": total_costs,
            "total_payroll_paid": payroll_total,
        })
