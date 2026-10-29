"""
Credit notes (against a sales invoice) and debit notes (against a purchase bill).

A note reduces the original document's outstanding balance and reverses its revenue/cost and tax in
proportion. Notes can never exceed the original's remaining value; the original is never modified.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from . import engine, gst
from .models import Voucher, VoucherTaxLine

ZERO = Decimal("0.00")
KIND_FOR = {"sales": "credit_note", "purchase": "debit_note"}


def noted(original):
    """Value already credited/debited against a document (posted, non-reversal notes only)."""
    return original.notes.filter(status="posted", reversal_of__isnull=True).aggregate(s=Sum("total_amount"))["s"] or ZERO


@transaction.atomic
def post_note(company, user, *, original_id, taxable_value, voucher_date, reason):
    if not reason:
        raise ValidationError("A reason is required.")
    taxable = Decimal(str(taxable_value)).quantize(ZERO)
    if taxable <= 0:
        raise ValidationError("Note value must be positive.")
    orig = Voucher.objects.select_for_update().filter(company=company, pk=original_id).first()
    if orig is None or orig.voucher_type not in KIND_FOR:
        raise ValidationError("Original must be a posted sales invoice or purchase bill.")
    if orig.is_opening:
        raise ValidationError("Opening documents have no revenue/cost in this ledger; adjust them with a journal.")
    if orig.status != "posted" or orig.reversal_of_id:
        raise ValidationError("Original document must be posted and not reversed.")
    party = orig.party_ledger
    tax_rows = list(VoucherTaxLine.objects.filter(voucher=orig))
    rate, seller, pos, itc, tc = ZERO, "", orig.place_of_supply, True, None
    if tax_rows:
        rate = sum((t.applied_rate for t in tax_rows if t.line_ref == tax_rows[0].line_ref), ZERO)
        seller, pos, itc, tc = tax_rows[0].seller_state, tax_rows[0].place_of_supply, tax_rows[0].itc_eligible, tax_rows[0].tax_code
        doc = gst.calculate_document([dict(quantity=1, unit_price=taxable, rate=rate)], seller_state=seller,
                                     place_of_supply=pos, round_to_rupee=False)
    else:
        doc = gst.calculate_document([dict(quantity=1, unit_price=taxable, rate=0)], seller_state="00",
                                     place_of_supply="00", round_to_rupee=False)
    total = doc["total"]
    if noted(orig) + total > orig.total_amount:
        raise ValidationError(f"Note total {total} exceeds the remaining value of {orig.voucher_number} "
                              f"({orig.total_amount - noted(orig)}).")
    comp = doc["component_totals"]
    lines = []
    if orig.voucher_type == "sales":      # credit note: Dr Sales Returns, Dr Output GST / Cr Receivable
        lines.append({"ledger": engine.mapped_ledger(company, "sales_returns"), "debit": taxable})
        for c in ("CGST", "SGST", "IGST"):
            if comp[c] > 0:
                lines.append({"ledger": engine.mapped_ledger(company, f"output_{c.lower()}"), "debit": comp[c]})
        lines.append({"ledger": party, "credit": total})
    else:                                 # debit note: Dr Payable / Cr Purchases, Cr Input GST (if credit was claimed)
        tax = doc["tax_total"]
        lines.append({"ledger": party, "debit": total})
        lines.append({"ledger": engine.mapped_ledger(company, "purchases"), "credit": taxable + (ZERO if itc else tax)})
        if itc:
            for c in ("CGST", "SGST", "IGST"):
                if comp[c] > 0:
                    lines.append({"ledger": engine.mapped_ledger(company, f"input_{c.lower()}"), "credit": comp[c]})
    tax_snap = [dict(tax_code=tc, line_ref="1", taxable_value=taxable, component=c["component"], applied_rate=c["rate"],
                     tax_amount=c["amount"], place_of_supply=pos, seller_state=seller, treatment="taxable", itc_eligible=itc,
                     snapshot={"note_against": orig.voucher_number}) for c in doc["lines"][0]["components"]]
    seq = orig.notes.count() + 1
    note = engine.create_voucher(
        company, user, voucher_type=KIND_FOR[orig.voucher_type], voucher_date=voucher_date, lines=lines, post=False,
        party_ledger=party, place_of_supply=pos, reference=orig.voucher_number, tax_lines=tax_snap,
        narration=f"{KIND_FOR[orig.voucher_type].replace('_', ' ').title()} against {orig.voucher_number}: {reason}",
        source={"module": "indian_accounting", "document_type": "note", "document_id": orig.id, "event": f"note{seq}"})
    note.against = orig
    note.save()
    return engine.post_voucher(note.pk, user, company)
