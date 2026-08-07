"""Vendor email drafting for purchase orders.

Composes a professional purchase-order email when orders are *placed*, not when
they are created. A PO sitting in draft or awaiting approval has not been
committed to the vendor, so it produces no email.

Grouping rule: one email per ordering action. Placing several orders for the
same vendor together — "Order All" over a multi-vendor selection — yields a
single consolidated email per vendor covering exactly that batch. Ordering
another PO for the same vendor later is a separate action and gets its own
email, so a vendor is never sent an amended copy of a message they already have.

No SMTP here by design — `send()` is deliberately absent. The model carries the
status/recipient/timestamp fields a future transport will need, so wiring SMTP
later means implementing delivery, not reshaping the data.
"""

from decimal import Decimal
from html import escape


def _money(value):
    return f"{Decimal(str(value or 0)):,.2f}"


def _order_block(po):
    """One order's heading and line-item table, for embedding in a draft."""
    rows = []
    for line in po.items.select_related("item").all():
        item = line.item
        rows.append(
            "<tr>"
            f'<td style="padding:8px 10px;border:1px solid #e2e8f0;">{escape(item.name)}</td>'
            f'<td style="padding:8px 10px;border:1px solid #e2e8f0;color:#64748b;">{escape(item.sku or "—")}</td>'
            f'<td style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">{line.quantity:g}</td>'
            f'<td style="padding:8px 10px;border:1px solid #e2e8f0;">{escape(item.unit or "unit")}</td>'
            f'<td style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">{_money(line.unit_price)}</td>'
            f'<td style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">{_money(line.total_price)}</td>'
            "</tr>"
        )

    if not rows:
        rows.append(
            '<tr><td colspan="6" style="padding:10px;border:1px solid #e2e8f0;'
            'color:#94a3b8;">No line items on this order.</td></tr>'
        )

    delivery = (
        f'<p style="margin:6px 0 0;color:#475569;font-size:13px;">'
        f"Requested delivery date: <strong>{po.expected_delivery:%d %b %Y}</strong></p>"
        if po.expected_delivery
        else ""
    )

    header = ", ".join(
        part for part in [
            f"Order date: {po.created_at:%d %b %Y}" if po.created_at else "",
            f"Priority: {po.priority}" if po.priority else "",
        ] if part
    )

    return f"""
<h3 style="margin:26px 0 4px;font-size:15px;color:#0f172a;">Purchase Order #PO-{po.id:04d}</h3>
<p style="margin:0;color:#64748b;font-size:12px;">{escape(header)}</p>
{delivery}
<table style="border-collapse:collapse;width:100%;margin-top:10px;font-size:13px;">
  <thead>
    <tr style="background:#f8fafc;">
      <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:left;">Item</th>
      <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:left;">SKU</th>
      <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">Qty</th>
      <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:left;">Unit</th>
      <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">Unit Price</th>
      <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">Total</th>
    </tr>
  </thead>
  <tbody>{"".join(rows)}</tbody>
</table>
<p style="margin:8px 0 0;text-align:right;font-size:13px;">
  Order total: <strong>{_money(po.total_amount)}</strong>
</p>
""".strip()


def compose_body(vendor, orders, company=None):
    """Build the full draft body covering every order for this vendor.

    Each order keeps its own heading and table so the vendor can act on them
    individually, while remaining one message.
    """
    company_name = getattr(company, "name", "") or "Our Company"
    grand_total = sum(Decimal(str(po.total_amount or 0)) for po in orders)
    blocks = "\n".join(_order_block(po) for po in orders)

    order_word = "order" if len(orders) == 1 else "orders"
    terms = (
        f'<p style="margin:16px 0 0;font-size:13px;">Payment terms: '
        f"<strong>{escape(vendor.payment_terms)}</strong></p>"
        if getattr(vendor, "payment_terms", "")
        else ""
    )
    total_line = (
        f'<p style="margin:18px 0 0;font-size:14px;text-align:right;">'
        f"<strong>Combined total across {len(orders)} orders: {_money(grand_total)}</strong></p>"
        if len(orders) > 1
        else ""
    )

    return f"""
<div style="font-family:Segoe UI,Arial,sans-serif;color:#0f172a;line-height:1.55;">
  <div style="border-bottom:2px solid #2563eb;padding-bottom:10px;margin-bottom:18px;">
    <h2 style="margin:0;font-size:18px;color:#1e3a8a;">{escape(company_name)}</h2>
    <p style="margin:2px 0 0;color:#64748b;font-size:12px;">Purchase Order</p>
  </div>

  <p style="margin:0 0 10px;">Dear {escape(vendor.name)},</p>
  <p style="margin:0 0 4px;">
    Please find below our purchase {order_word}. Kindly confirm receipt, availability
    and the expected despatch date at your earliest convenience.
  </p>

  {blocks}
  {total_line}
  {terms}

  <p style="margin:18px 0 0;font-size:13px;">
    Please quote the purchase order number on all correspondence, packing notes and invoices.
    Deliveries should be accompanied by a delivery note listing the order number and quantities supplied.
  </p>

  <p style="margin:18px 0 0;">Kind regards,</p>
  <p style="margin:2px 0 0;"><strong>Procurement Team</strong><br>
  <span style="color:#64748b;font-size:13px;">{escape(company_name)}</span></p>
</div>
""".strip()


def compose_subject(vendor, orders, company=None):
    company_name = getattr(company, "name", "") or "Purchase Order"
    if len(orders) == 1:
        return f"Purchase Order #PO-{orders[0].id:04d} — {company_name}"
    numbers = ", ".join(f"PO-{po.id:04d}" for po in orders)
    return f"Purchase Orders {numbers} — {company_name}"


def draft_for_orders(orders):
    """Draft one email per vendor for a batch of newly-placed orders.

    `orders` is everything placed in a single ordering action. Orders are
    grouped by vendor, so a mixed-vendor batch produces one email each, and
    several orders to one vendor produce a single consolidated email.

    Orders already covered by an email are skipped — re-ordering something does
    not re-notify the vendor. Vendors with no address on file are skipped too:
    there is nobody to write to, and an empty recipient would only clutter the
    Mail Center.

    Returns the list of VendorEmails created.
    """
    from .models import VendorEmail

    by_vendor = {}
    for po in orders:
        vendor = po.vendor
        if not vendor or not vendor.email:
            continue
        if po.emails.exists():
            continue
        by_vendor.setdefault(vendor.id, (vendor, []))[1].append(po)

    created = []
    for vendor, vendor_orders in by_vendor.values():
        company = getattr(vendor, "company", None)
        vendor_orders.sort(key=lambda p: p.id)

        draft = VendorEmail.objects.create(
            company=company,
            vendor=vendor,
            to_email=vendor.email,
            subject=compose_subject(vendor, vendor_orders, company),
            body_html=compose_body(vendor, vendor_orders, company),
        )
        draft.purchase_orders.set(vendor_orders)
        created.append(draft)

    return created


def draft_for_purchase_order(po):
    """Draft the email for a single placed order.

    Thin wrapper over `draft_for_orders` for callers handling one order at a
    time. Returns the VendorEmail, or None when nothing was drafted.
    """
    drafts = draft_for_orders([po])
    return drafts[0] if drafts else None
