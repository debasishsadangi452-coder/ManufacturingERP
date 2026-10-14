"""Order- and invoice-level discount and tax arithmetic.

An order may carry percentage discounts (several named columns) and an
optional tax amount. Invoices raised from it — one total invoice, or one per
dispatch — carry the same discount percentage and a pro-rata share of the
tax, so split invoices always add back up to the order.
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def order_adjustments(subtotal, percents, tax_amount):
    """-> (discount_amount, tax_amount, total) for a subtotal."""
    subtotal = money(subtotal)
    discount = money(subtotal * sum((Decimal(str(p)) for p in percents), Decimal("0")) / Decimal("100"))
    discount = min(discount, subtotal)
    tax = money(tax_amount)
    return discount, tax, subtotal - discount + tax


def invoice_adjustments(order, invoice_subtotal):
    """Discount and tax for an invoice covering `invoice_subtotal` of `order`.

    Discount uses the order's combined percentage. Tax is the order's tax
    amount pro-rated by the share of the order subtotal this invoice covers,
    and never exceeds what is still untaxed on earlier invoices.
    """
    invoice_subtotal = money(invoice_subtotal)
    percent = order.discount_percent
    discount = min(money(invoice_subtotal * percent / Decimal("100")), invoice_subtotal)
    tax = Decimal("0")
    if order.tax_amount and order.subtotal:
        already = sum((i.tax_amount for i in order.invoices.exclude(status="cancelled")), Decimal("0"))
        share = money(order.tax_amount * invoice_subtotal / order.subtotal)
        tax = max(min(share, order.tax_amount - already), Decimal("0"))
    return discount, tax, invoice_subtotal - discount + tax


def apply_to_invoice(invoice, order, invoice_subtotal):
    """Stamp subtotal/discount/tax/total on a freshly created invoice."""
    discount, tax, total = invoice_adjustments(order, invoice_subtotal)
    invoice.subtotal = money(invoice_subtotal)
    invoice.discount_amount = discount
    invoice.tax_amount = tax
    invoice.total_amount = total
    invoice.save(update_fields=["subtotal", "discount_amount", "tax_amount", "total_amount"])
    return invoice
