"""
GST calculation service (calculation only - no portal/filing integration).

Pure decimal arithmetic, no database writes. Place of supply must be supplied
explicitly; missing/conflicting inputs raise ``ValidationError`` rather than
guessing the tax treatment.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError

TWO = Decimal("0.01")
HUNDRED = Decimal("100")


def q(value, places=TWO):
    return Decimal(value).quantize(places, rounding=ROUND_HALF_UP)


def calculate_line(*, quantity, unit_price, rate, seller_state, place_of_supply,
                   discount=Decimal("0"), charges=Decimal("0"), treatment="taxable",
                   price_includes_tax=False, reverse_charge=False):
    """Return a dict describing the tax split for one invoice line."""
    if not seller_state or not place_of_supply:
        raise ValidationError("Seller state and place of supply are required to determine GST.")
    quantity, unit_price = Decimal(str(quantity)), Decimal(str(unit_price))
    discount, charges = Decimal(str(discount)), Decimal(str(charges))
    rate = Decimal(str(rate))
    if quantity <= 0 or unit_price < 0 or discount < 0 or charges < 0:
        raise ValidationError("Quantity must be positive; price, discount and charges cannot be negative.")

    gross = quantity * unit_price
    if discount > gross:
        raise ValidationError("Discount cannot exceed the line value.")

    if treatment != "taxable":
        rate = Decimal("0")

    if price_includes_tax:
        total_incl = gross - discount + charges
        taxable = q(total_incl * HUNDRED / (HUNDRED + rate))
    else:
        taxable = q(gross - discount + charges)

    intra = str(seller_state) == str(place_of_supply)
    components = []
    if rate > 0:
        if intra:
            half = rate / 2
            cgst = q(taxable * half / HUNDRED)
            sgst = q(taxable * half / HUNDRED)
            components = [("CGST", half, cgst), ("SGST", half, sgst)]
        else:
            components = [("IGST", rate, q(taxable * rate / HUNDRED))]
    tax_total = sum((c[2] for c in components), Decimal("0.00"))
    return {
        "taxable_value": taxable,
        "rate": rate,
        "intra_state": intra,
        "treatment": treatment,
        "reverse_charge": reverse_charge,
        "components": [{"component": c, "rate": r, "amount": a} for c, r, a in components],
        "tax_total": tax_total,
        "line_total": taxable + (Decimal("0.00") if reverse_charge else tax_total),
    }


def calculate_document(lines, *, seller_state, place_of_supply, round_to_rupee=True, **kwargs):
    """Aggregate several lines; adds a round-off so the total is whole rupees."""
    results = [calculate_line(seller_state=seller_state, place_of_supply=place_of_supply, **l, **kwargs) for l in lines]
    taxable = sum((r["taxable_value"] for r in results), Decimal("0.00"))
    tax = sum((r["tax_total"] for r in results), Decimal("0.00"))
    totals = {"CGST": Decimal("0.00"), "SGST": Decimal("0.00"), "IGST": Decimal("0.00")}
    for r in results:
        for c in r["components"]:
            totals[c["component"]] += c["amount"]
    subtotal = sum((r["line_total"] for r in results), Decimal("0.00"))
    total = subtotal.quantize(Decimal("1"), rounding=ROUND_HALF_UP) if round_to_rupee else subtotal
    return {
        "lines": results, "taxable_value": taxable, "tax_total": tax, "component_totals": totals,
        "round_off": q(total - subtotal), "total": q(total),
    }


def resolve_tax_code(company, code, on_date):
    """Return the effective TaxCode for ``code`` on ``on_date``."""
    from django.db.models import Q
    from .models import TaxCode
    tc = (TaxCode.objects.filter(company=company, code=code, is_active=True, effective_from__lte=on_date)
          .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on_date))
          .order_by("-effective_from").first())
    if tc is None:
        raise ValidationError(f"No active tax code '{code}' effective on {on_date}.")
    return tc
