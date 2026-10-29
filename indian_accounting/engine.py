"""
Double-entry posting engine for the Indian accounting system.

All financial writes go through here. Posting is atomic, balanced, period-aware,
idempotent (DB-level unique constraints) and audited. Posted vouchers are never
edited or deleted; ``reverse_voucher`` creates a linked mirror voucher instead.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (AccountingPeriod, AuditLog, FinancialYear, Ledger, LedgerMapping, TaxCode,
                     Voucher, VoucherLine, VoucherSequence, VoucherTaxLine)

ZERO = Decimal("0.00")
PREFIX = {"journal": "JV", "payment": "PV", "receipt": "RV", "contra": "CV", "sales": "SI",
          "purchase": "PB", "credit_note": "CN", "debit_note": "DN"}


def audit(company, user, entity_type, entity_id, action, before=None, after=None, reason=""):
    return AuditLog.objects.create(
        company=company, user=user if (user and user.is_authenticated) else None,
        entity_type=entity_type, entity_id=str(entity_id), action=action,
        before=before, after=after, reason=reason)


def mapped_ledger(company, key):
    try:
        return LedgerMapping.objects.select_related("ledger").get(company=company, key=key).ledger
    except LedgerMapping.DoesNotExist:
        raise ValidationError(f"Default ledger '{key}' is not configured.")


def find_period(company, on_date):
    fy = FinancialYear.objects.filter(company=company, start_date__lte=on_date, end_date__gte=on_date).first()
    if fy is None:
        raise ValidationError(f"No financial year covers {on_date}.")
    if fy.status == "closed":
        raise ValidationError(f"Financial year {fy.name} is closed.")
    period = AccountingPeriod.objects.filter(company=company, start_date__lte=on_date, end_date__gte=on_date).first()
    if period is not None and period.status == "locked":
        raise ValidationError(f"Accounting period starting {period.start_date} is locked.")
    return fy, period


def _next_number(company, fy, vtype):
    seq, _ = VoucherSequence.objects.select_for_update().get_or_create(
        company=company, financial_year=fy, voucher_type=vtype, defaults={"prefix": PREFIX.get(vtype, "V")})
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"{seq.prefix}/{fy.name}/{seq.last_number:05d}"


def _q(v):
    return Decimal(str(v)).quantize(ZERO)


def validate_lines(company, lines):
    """lines: [{ledger: Ledger|id, debit, credit, narration}] -> normalised list."""
    if len(lines) < 2:
        raise ValidationError("A voucher needs at least two lines.")
    out, dr, cr = [], ZERO, ZERO
    for i, ln in enumerate(lines, 1):
        ledger = ln["ledger"]
        if not isinstance(ledger, Ledger):
            ledger = Ledger.objects.filter(company=company, pk=ledger).first()
        if ledger is None or ledger.company_id != company.id:
            raise ValidationError(f"Line {i}: ledger not found for this company.")
        if not ledger.is_active:
            raise ValidationError(f"Line {i}: ledger '{ledger.name}' is inactive.")
        d, c = _q(ln.get("debit") or 0), _q(ln.get("credit") or 0)
        if d < 0 or c < 0 or (d > 0) == (c > 0):
            raise ValidationError(f"Line {i}: exactly one of debit or credit must be a positive amount.")
        dr, cr = dr + d, cr + c
        out.append({"ledger": ledger, "debit": d, "credit": c, "narration": ln.get("narration", "")})
    if dr != cr:
        raise ValidationError(f"Voucher is not balanced: debit {dr} != credit {cr}.")
    return out, dr


@transaction.atomic
def create_voucher(company, user, *, voucher_type, voucher_date, lines, narration="", reference="",
                   party_ledger=None, place_of_supply="", source=None, idempotency_key=None,
                   supplier_invoice_no="", tax_lines=None, post=False, reversal_of=None, due_date=None):
    """Create a draft voucher (optionally posting it immediately). Idempotent on key/source."""
    if idempotency_key:
        existing = Voucher.objects.filter(company=company, idempotency_key=idempotency_key).first()
        if existing:
            return existing
    source = source or {}
    if source.get("document_id"):
        existing = Voucher.objects.filter(
            company=company, source_module=source.get("module", ""), source_document_type=source.get("document_type", ""),
            source_document_id=str(source["document_id"]), source_event=source.get("event", ""),
            status__in=["draft", "submitted", "approved", "posted"]).first()
        if existing:
            return existing
    fy, period = find_period(company, voucher_date)
    norm, total = validate_lines(company, lines)
    if voucher_type == "purchase" and supplier_invoice_no and party_ledger:
        if Voucher.objects.filter(company=company, voucher_type="purchase", party_ledger=party_ledger,
                                  supplier_invoice_no=supplier_invoice_no).exclude(status__in=["cancelled", "reversed"]).exists():
            raise ValidationError("Duplicate supplier invoice number for this supplier.")
    try:
        with transaction.atomic():
            v = Voucher.objects.create(
                company=company, financial_year=fy, period=period, voucher_type=voucher_type,
                voucher_date=voucher_date, narration=narration, reference=reference,
                party_ledger=party_ledger, place_of_supply=place_of_supply, total_amount=total, due_date=due_date,
                idempotency_key=idempotency_key, supplier_invoice_no=supplier_invoice_no,
                source_module=source.get("module", ""), source_document_type=source.get("document_type", ""),
                source_document_id=str(source.get("document_id", "")), source_event=source.get("event", ""),
                created_by=user if (user and user.is_authenticated) else None, reversal_of=reversal_of)
            VoucherLine.objects.bulk_create([
                VoucherLine(voucher=v, line_number=i, ledger=l["ledger"], debit=l["debit"], credit=l["credit"],
                            narration=l["narration"], company=company, voucher_date=voucher_date)
                for i, l in enumerate(norm, 1)])
            for t in tax_lines or []:
                VoucherTaxLine.objects.create(voucher=v, **t)
            audit(company, user, "voucher", v.pk, "create", after={"type": voucher_type, "total": str(total)})
    except IntegrityError:
        raise ValidationError("Duplicate posting detected for this source document or idempotency key.")
    if post:
        v = post_voucher(v.pk, user, company)
    return v


@transaction.atomic
def post_voucher(voucher_id, user, company, approval_required=False):
    v = Voucher.objects.select_for_update().filter(company=company, pk=voucher_id).first()
    if v is None:
        raise ValidationError("Voucher not found.")
    if v.status == "posted":
        raise ValidationError("Voucher is already posted.")
    if v.status in ("reversed", "cancelled"):
        raise ValidationError(f"Cannot post a {v.status} voucher.")
    if approval_required and v.status != "approved":
        raise ValidationError("Voucher requires approval before posting.")
    fy, period = find_period(company, v.voucher_date)
    lines = list(v.lines.select_related("ledger"))
    validate_lines(company, [{"ledger": l.ledger, "debit": l.debit, "credit": l.credit} for l in lines])
    if not v.voucher_number:
        v.voucher_number = _next_number(company, fy, v.voucher_type)
    v.financial_year, v.period = fy, period
    v.status, v.posted_at = "posted", timezone.now()
    v.posted_by = user if (user and user.is_authenticated) else None
    v._allow_status_change = True
    v.save()
    VoucherLine.objects.filter(voucher=v).update(is_posted=True)
    audit(company, user, "voucher", v.pk, "post", after={"number": v.voucher_number})
    return v


@transaction.atomic
def submit_voucher(voucher_id, user, company):
    v = Voucher.objects.select_for_update().get(company=company, pk=voucher_id)
    if v.status != "draft":
        raise ValidationError("Only draft vouchers can be submitted.")
    v.status = "submitted"
    v.save()
    audit(company, user, "voucher", v.pk, "submit")
    return v


@transaction.atomic
def approve_voucher(voucher_id, user, company, allow_self_approval=False):
    v = Voucher.objects.select_for_update().get(company=company, pk=voucher_id)
    if v.status != "submitted":
        raise ValidationError("Only submitted vouchers can be approved.")
    if not allow_self_approval and v.created_by_id == getattr(user, "id", None):
        raise ValidationError("The creator cannot approve their own voucher.")
    v.status, v.approved_by = "approved", user
    v.save()
    audit(company, user, "voucher", v.pk, "approve")
    return v


@transaction.atomic
def reverse_voucher(voucher_id, user, company, reason, reverse_date=None):
    """Post a mirror voucher; the original stays intact and is flagged ``reversed``."""
    if not reason:
        raise ValidationError("A reason is required to reverse a voucher.")
    orig = Voucher.objects.select_for_update().filter(company=company, pk=voucher_id).first()
    if orig is None:
        raise ValidationError("Voucher not found.")
    if orig.status != "posted":
        raise ValidationError("Only posted vouchers can be reversed.")
    if orig.reversal_of_id:
        raise ValidationError("A reversal voucher cannot itself be reversed.")
    if orig.is_opening:
        raise ValidationError("Opening documents carry no ledger entries; correct them through the opening balances.")
    lines = [{"ledger": l.ledger, "debit": l.credit, "credit": l.debit, "narration": f"Reversal of {orig.voucher_number}"}
             for l in orig.lines.select_related("ledger")]
    rev = create_voucher(company, user, voucher_type=orig.voucher_type, voucher_date=reverse_date or orig.voucher_date,
                         lines=lines, narration=f"Reversal of {orig.voucher_number}: {reason}", reference=orig.voucher_number,
                         party_ledger=orig.party_ledger, place_of_supply=orig.place_of_supply, reversal_of=orig, post=True)
    orig._allow_status_change = True
    orig.status = "reversed"
    orig.save()
    from .allocations import release_for_voucher
    release_for_voucher(orig, user, "Voucher reversed")
    from .valuation import void_for_voucher
    void_for_voucher(orig)
    audit(company, user, "voucher", orig.pk, "reverse", reason=reason, after={"reversal": rev.voucher_number})
    return rev


@transaction.atomic
def post_tax_invoice(company, user, *, kind, voucher_date, party_ledger, place_of_supply, doc, seller_state,
                     source=None, supplier_invoice_no="", narration="", post=True, tax_code=None, idempotency_key=None,
                     itc_eligible=None, grni_clear=ZERO, reference="", due_date=None):
    """Sales invoice / purchase bill from a ``gst.calculate_document`` result.

    Sales:    Dr Receivable(party)  Cr Sales, Output GST (+/- round-off)
    Purchase: Dr Purchases, Input GST (ITC-eligible) / Cr Payable(party)
    Ineligible input GST is capitalised into the purchase cost.
    """
    if kind not in ("sales", "purchase"):
        raise ValidationError("kind must be 'sales' or 'purchase'.")
    taxable, comp, round_off, total = doc["taxable_value"], doc["component_totals"], doc["round_off"], doc["total"]
    itc_ok = itc_eligible if itc_eligible is not None else (tax_code.itc_eligible if tax_code is not None else True)
    lines = []
    if kind == "sales":
        lines.append({"ledger": party_ledger, "debit": total})
        lines.append({"ledger": mapped_ledger(company, "sales"), "credit": taxable})
        for c in ("CGST", "SGST", "IGST"):
            if comp[c] > 0:
                lines.append({"ledger": mapped_ledger(company, f"output_{c.lower()}"), "credit": comp[c]})
        if round_off > 0:
            lines.append({"ledger": mapped_ledger(company, "round_off"), "credit": round_off})
        elif round_off < 0:
            lines.append({"ledger": mapped_ledger(company, "round_off"), "debit": -round_off})
    else:
        purchase_cost = taxable
        for c in ("CGST", "SGST", "IGST"):
            if comp[c] > 0 and not itc_ok:
                purchase_cost += comp[c]
        clear = min(Decimal(grni_clear), purchase_cost)  # goods already received sit in stock against GRNI
        if clear > 0:
            lines.append({"ledger": mapped_ledger(company, "grni"), "debit": clear})
        if purchase_cost - clear > 0:
            lines.append({"ledger": mapped_ledger(company, "purchases"), "debit": purchase_cost - clear})
        for c in ("CGST", "SGST", "IGST"):
            if comp[c] > 0 and itc_ok:
                lines.append({"ledger": mapped_ledger(company, f"input_{c.lower()}"), "debit": comp[c]})
        lines.append({"ledger": party_ledger, "credit": total})
        if round_off > 0:
            lines.append({"ledger": mapped_ledger(company, "round_off"), "debit": round_off})
        elif round_off < 0:
            lines.append({"ledger": mapped_ledger(company, "round_off"), "credit": -round_off})
    tax_rows = []
    for i, ln in enumerate(doc["lines"], 1):
        for c in ln["components"]:
            tax_rows.append(dict(
                tax_code=tax_code, line_ref=str(i), taxable_value=ln["taxable_value"], component=c["component"],
                applied_rate=c["rate"], tax_amount=c["amount"], place_of_supply=place_of_supply,
                seller_state=seller_state, treatment=ln["treatment"], itc_eligible=itc_ok,
                snapshot={"rate": str(ln["rate"]), "intra_state": ln["intra_state"],
                          "reverse_charge": ln["reverse_charge"]}))
    return create_voucher(
        company, user, voucher_type=kind, voucher_date=voucher_date, lines=lines, party_ledger=party_ledger,
        place_of_supply=place_of_supply, source=source, supplier_invoice_no=supplier_invoice_no,
        narration=narration, tax_lines=tax_rows, post=post, idempotency_key=idempotency_key, reference=reference,
        due_date=due_date)


def ledger_balance(ledger, as_of=None):
    """Signed balance (Dr positive) from opening + posted lines."""
    from django.db.models import Sum
    qs = VoucherLine.objects.filter(ledger=ledger, is_posted=True)
    if as_of:
        qs = qs.filter(voucher_date__lte=as_of)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    opening = ledger.opening_balance if ledger.opening_side == "Dr" else -ledger.opening_balance
    return opening + (agg["d"] or ZERO) - (agg["c"] or ZERO)
