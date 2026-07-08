from decimal import Decimal

from django.utils import timezone

from .models import DepartmentBudget, ExpenseRequest, OperationalCost


def record_procurement_cost(po, user=None):
    """Book a received purchase order into finance:

    1. An auto-approved ExpenseRequest against the active procurement
       budget, so the departmental allocation bars reflect the spend.
    2. An OperationalCost ledger entry (linked to that expense), so the
       dashboard's operational cost total reflects it too.

    Idempotent: keyed on invoice/reference "PO-<id>" per company, so
    receiving the same PO twice never double-books the spend.
    """
    ref = f"PO-{po.id}"
    company = po.vendor.company
    if OperationalCost.objects.filter(invoice_number=ref, company=company).exists():
        return None

    amount = po.total_amount or Decimal("0")
    title = f"Goods received - PO #{po.id} ({po.vendor.name})"

    # Prefer the active monthly procurement budget, else any active one.
    budget = (
        DepartmentBudget.objects.filter(
            company=company, department="procurement", is_active=True, period="monthly"
        ).order_by("-created_at").first()
        or DepartmentBudget.objects.filter(
            company=company, department="procurement", is_active=True
        ).order_by("-created_at").first()
    )

    expense = ExpenseRequest.objects.create(
        title=title,
        category="raw_material",
        amount=amount,
        budget=budget,
        requested_by=user,
        vendor=po.vendor.name,
        reference_number=ref,
        status="auto_approved",
        notes="Auto-booked from goods receipt.",
        company=company,
    )
    return OperationalCost.objects.create(
        title=title,
        cost_type="variable",
        department="procurement",
        amount=amount,
        date=timezone.localdate(),
        vendor=po.vendor.name,
        invoice_number=ref,
        expense_request=expense,
        recorded_by=user,
        company=company,
    )
