from decimal import Decimal

from django.utils import timezone

from .models import OperationalCost


def record_procurement_cost(po, user=None):
    """Book a received purchase order as an operational cost so procurement
    spend shows up on the finance dashboard and the costs ledger.

    Idempotent: keyed on invoice_number "PO-<id>" so receiving the same PO
    twice never double-books the spend.
    """
    ref = f"PO-{po.id}"
    company = po.vendor.company
    if OperationalCost.objects.filter(invoice_number=ref, company=company).exists():
        return None
    return OperationalCost.objects.create(
        title=f"Goods received - PO #{po.id} ({po.vendor.name})",
        cost_type="variable",
        department="procurement",
        amount=po.total_amount or Decimal("0"),
        date=timezone.localdate(),
        vendor=po.vendor.name,
        invoice_number=ref,
        recorded_by=user,
        company=company,
    )
