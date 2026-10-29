"""
Invoice-level settlement, ageing and party statements.

Receipts/payments are never auto-allocated to the oldest invoice: an allocation is
either explicit (API) or follows a direct ERP link (a customer payment tied to its
invoice). Whatever is not allocated stays visible as an advance / unallocated amount.
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Allocation, Voucher, VoucherLine

ZERO = Decimal("0.00")
# invoice voucher type -> (settling voucher type, ledger side the party sits on in the invoice)
SETTLES = {"sales": "receipt", "purchase": "payment"}
BUCKETS = [("current", None, 0), ("1-30", 1, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, None)]


def _live_invoice_qs(company, kind):
    return Voucher.objects.filter(company=company, voucher_type=kind, status="posted",
                                  reversal_of__isnull=True, party_ledger__isnull=False)


def allocated_to(invoice):
    return invoice.allocations_received.filter(status="active").aggregate(s=Sum("amount"))["s"] or ZERO


def allocated_from(payment):
    return payment.allocations_made.filter(status="active").aggregate(s=Sum("amount"))["s"] or ZERO


def payment_amount(payment):
    """Amount a receipt/payment settles on the party ledger (credit for receipts, debit for payments)."""
    agg = VoucherLine.objects.filter(voucher=payment, ledger=payment.party_ledger).aggregate(
        d=Sum("debit"), c=Sum("credit"))
    return (agg["c"] if payment.voucher_type == "receipt" else agg["d"]) or ZERO


def outstanding(invoice):
    from .notes import noted
    return invoice.total_amount - allocated_to(invoice) - noted(invoice)


def unallocated(payment):
    return payment_amount(payment) - allocated_from(payment)


@transaction.atomic
def allocate(company, user, *, payment_id, invoice_id, amount, on_date=None):
    amount = Decimal(str(amount)).quantize(ZERO)
    if amount <= 0:
        raise ValidationError("Allocation amount must be positive.")
    # Lock in a stable order so concurrent allocations against the same rows serialise.
    rows = {v.pk: v for v in Voucher.objects.select_for_update().filter(company=company, pk__in=[payment_id, invoice_id]).order_by("pk")}
    pay, inv = rows.get(payment_id), rows.get(invoice_id)
    if pay is None or inv is None:
        raise ValidationError("Payment or invoice not found.")
    if inv.voucher_type not in SETTLES or pay.voucher_type != SETTLES[inv.voucher_type]:
        raise ValidationError("A receipt settles a sales invoice and a payment settles a purchase bill.")
    if pay.status != "posted" or inv.status != "posted" or pay.reversal_of_id or inv.reversal_of_id:
        raise ValidationError("Both documents must be posted and not reversals.")
    if not pay.party_ledger_id or pay.party_ledger_id != inv.party_ledger_id:
        raise ValidationError("Payment and invoice belong to different parties.")
    if amount > outstanding(inv):
        raise ValidationError(f"Amount exceeds the invoice's outstanding balance ({outstanding(inv)}).")
    if amount > unallocated(pay):
        raise ValidationError(f"Amount exceeds the payment's unallocated balance ({unallocated(pay)}).")
    a = Allocation.objects.create(company=company, payment_voucher=pay, invoice_voucher=inv, amount=amount,
                                  allocation_date=on_date or timezone.localdate(),
                                  created_by=user if getattr(user, "is_authenticated", False) else None)
    from .engine import audit
    audit(company, user, "allocation", a.pk, "allocate",
          after={"payment": pay.voucher_number, "invoice": inv.voucher_number, "amount": str(amount)})
    return a


@transaction.atomic
def release(company, user, allocation_id, reason):
    if not reason:
        raise ValidationError("A reason is required to release an allocation.")
    a = Allocation.objects.select_for_update().filter(company=company, pk=allocation_id).first()
    if a is None:
        raise ValidationError("Allocation not found.")
    if a.status != "active":
        raise ValidationError("Allocation is already released.")
    a.status, a.released_at, a.release_reason = "released", timezone.now(), reason[:200]
    a.released_by = user if getattr(user, "is_authenticated", False) else None
    a.save()
    from .engine import audit
    audit(company, user, "allocation", a.pk, "release", reason=reason)
    return a


def release_for_voucher(voucher, user, reason):
    """Called when a payment or invoice is reversed: its settlements no longer stand."""
    live = Allocation.objects.filter(status="active").filter(payment_voucher=voucher) | \
        Allocation.objects.filter(status="active", invoice_voucher=voucher)
    for a in live:
        release(voucher.company, user, a.pk, reason)


def open_items(company, kind, as_of=None):
    """Posted invoices/bills with a balance, oldest due first."""
    as_of = as_of or timezone.localdate()
    rows = []
    for inv in _live_invoice_qs(company, kind).filter(voucher_date__lte=as_of).select_related("party_ledger"):
        paid = inv.allocations_received.filter(status="active", allocation_date__lte=as_of).aggregate(s=Sum("amount"))["s"] or ZERO
        from .notes import noted
        paid += inv.notes.filter(status="posted", reversal_of__isnull=True, voucher_date__lte=as_of).aggregate(
            s=Sum("total_amount"))["s"] or ZERO
        bal = inv.total_amount - paid
        if bal > 0:
            due = inv.due_date or inv.voucher_date
            rows.append({"voucher_id": inv.id, "number": inv.voucher_number, "party_id": inv.party_ledger_id,
                         "party": inv.party_ledger.name, "date": inv.voucher_date, "due_date": due,
                         "total": inv.total_amount, "paid": paid, "outstanding": bal,
                         "days_overdue": max((as_of - due).days, 0)})
    return sorted(rows, key=lambda r: (r["due_date"], r["number"]))


def unallocated_payments(company, kind, as_of=None):
    """Advances / unallocated receipts (kind=sales) or payments (kind=purchase)."""
    as_of = as_of or timezone.localdate()
    out = []
    qs = Voucher.objects.filter(company=company, voucher_type=SETTLES[kind], status="posted", reversal_of__isnull=True,
                                party_ledger__isnull=False, voucher_date__lte=as_of).select_related("party_ledger")
    for p in qs:
        left = unallocated(p)
        if left > 0:
            out.append({"voucher_id": p.id, "number": p.voucher_number, "party_id": p.party_ledger_id,
                        "party": p.party_ledger.name, "date": p.voucher_date, "unallocated": left})
    return out


def ageing(company, kind, as_of=None):
    items = open_items(company, kind, as_of)
    parties = {}
    for it in items:
        p = parties.setdefault(it["party_id"], {"party": it["party"], "buckets": {b[0]: ZERO for b in BUCKETS}, "total": ZERO})
        for name, lo, hi in BUCKETS:
            d = it["days_overdue"]
            if (lo is None or d >= lo) and (hi is None or d <= hi) and (name != "current" or d == 0):
                p["buckets"][name] += it["outstanding"]
                break
        p["total"] += it["outstanding"]
    advances = {}
    for u in unallocated_payments(company, kind, as_of):
        advances[u["party_id"]] = advances.get(u["party_id"], ZERO) + u["unallocated"]
    for pid, p in parties.items():
        p["advance"] = advances.get(pid, ZERO)
    totals = {b[0]: sum((p["buckets"][b[0]] for p in parties.values()), ZERO) for b in BUCKETS}
    return {"as_of": as_of or timezone.localdate(), "parties": list(parties.values()), "totals": totals,
            "grand_total": sum(totals.values(), ZERO), "unallocated": unallocated_payments(company, kind, as_of)}


def statement(company, ledger, date_from, date_to):
    from .reports import general_ledger
    kind = "sales" if ledger.control_type == "receivable" else "purchase" if ledger.control_type == "payable" else None
    gl = general_ledger(company, ledger, date_from, date_to)
    if kind:
        gl["open_items"] = [i for i in open_items(company, kind, date_to) if i["party_id"] == ledger.id]
        gl["unallocated"] = [u for u in unallocated_payments(company, kind, date_to) if u["party_id"] == ledger.id]
    return gl
