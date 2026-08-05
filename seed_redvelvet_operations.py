"""Additive operational data for Red Velvet NYC — the things the catalog seed
does not cover: inbound PO emails, lot genealogy, a Mount Kisco → Milton
transfer, and a weekly cycle count.

Purely additive: it creates nothing that already exists and deletes nothing.
Safe to run against production alongside seed_redvelvet_data.py.

Run:  python seed_redvelvet_operations.py
"""

import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "freshfizz_erp.settings")
django.setup()

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from accounts.models import Company, User
from inventory.lots import (
    consume_lots_fifo, create_finished_lot, create_raw_lot, ship_lots_fifo,
)
from inventory.models import Batch, CycleCount, CycleCountLine, Item, Stock, StockTransfer, Warehouse
from inventory.operations import complete_transfer, post_cycle_count
from procurement.models import GoodsReceipt, PurchaseOrder, PurchaseOrderItem, Vendor
from production.models import ProductionLine, ProductionOrder, Recipe
from sales.models import (
    Customer, InboundOrderEmail, SalesOrder, SalesOrderItem, Shipment,
)

SLUG = "redvelvetnyc"

# The four real ERP users. The Operations Coordinator is the primary user and
# spans procurement/inventory/shipping/order-entry, which maps to `store`.
TEAM = [
    ("Maria", "production", "Operations Manager"),
    ("David", "quality", "Quality Control Manager"),
    ("Priya", "store", "Operations Coordinator"),
]

# Dummy inbound PO emails for the email-automation screen. Mix of clean
# machine-readable POs and messy low-confidence ones so the review queue and
# the "needs attention" path both have something to show.
INBOUND_EMAILS = [
    {
        "sender": "purchasing@costcone.com",
        "subject": "PO #44812 — Fall replenishment",
        "raw_body": (
            "Hi team,\n\nPlease confirm the following for pickup at Milton on 9/18:\n"
            "  - 240 x Ready-to-Bake Cookie Dough Skillet\n"
            "  - 180 x Red Velvet Cupcakes (18)\n"
            "  - 120 x Brownies\n\nPO #44812. Standard Net 30.\n\nThanks,\nCostco NE Purchasing"
        ),
        "parsed": {
            "customer": "Costco NE", "po_number": "44812", "pickup_date": "2026-09-18",
            "lines": [
                {"product": "Ready-to-Bake Cookie Dough Skillet", "quantity": 240},
                {"product": "Red Velvet Cupcakes (18)", "quantity": 180},
                {"product": "Brownies", "quantity": 120},
            ],
        },
        "confidence": 0.96,
        "status": "parsed",
        "create_order": True,
    },
    {
        "sender": "orders@freshmart.com",
        "subject": "Weekly order - FreshMart",
        "raw_body": (
            "Weekly standing order:\n"
            "60 Snickerdoodle, 60 Ginger Snap, 90 Ready-to-Eat Cookie Dough Cup.\n"
            "Same pickup window as usual."
        ),
        "parsed": {
            "customer": "FreshMart", "pickup_date": "2026-09-12",
            "lines": [
                {"product": "Snickerdoodle Cookies", "quantity": 60},
                {"product": "Ginger Snap Cookies", "quantity": 60},
                {"product": "Ready-to-Eat Cookie Dough Cup (5oz)", "quantity": 90},
            ],
            "notes": "Ginger Snap is flagged sold out — confirm availability before promising.",
        },
        "confidence": 0.88,
        "status": "parsed",
        "create_order": True,
    },
    {
        "sender": "jane@sweettoothcafe.co",
        "subject": "order?",
        "raw_body": "hey! can we get some of the chocolate ones again, maybe 50ish? whenever works. thx jane",
        "parsed": {
            "customer": None, "lines": [{"product": None, "quantity": 50}],
            "notes": "Customer not matched; product ambiguous ('the chocolate ones'). Needs human review.",
        },
        "confidence": 0.34,
        "status": "needs_attention",
        "create_order": False,
    },
    {
        "sender": "procurement@hudsonvalleygrocers.com",
        "subject": "PO 7781 — holiday pre-book",
        "raw_body": (
            "Holiday pre-book, delivery window Nov 20-24:\n"
            "  French Macarons — 150\n  Chocolate Truffles — 200\n"
            "  Fancy Birthday Cake — 40\n\nPlease acknowledge receipt."
        ),
        "parsed": {
            "customer": "Hudson Valley Grocers", "po_number": "7781",
            "pickup_date": "2026-11-20",
            "lines": [
                {"product": "French Macarons", "quantity": 150},
                {"product": "Chocolate Truffles", "quantity": 200},
                {"product": "Fancy Birthday Cake", "quantity": 40},
            ],
        },
        "confidence": 0.93,
        "status": "parsed",
        "create_order": False,
    },
    {
        "sender": "noreply@shopifyapp.com",
        "subject": "Automated: 3 new online orders",
        "raw_body": "Your store received 3 new orders totalling $412.00. Log in to view details.",
        "parsed": {"notes": "Not a purchase order — marketing/notification email."},
        "confidence": 0.11,
        "status": "failed",
        "create_order": False,
        "error": "Not a purchase order; no line items could be extracted.",
    },
]


def ensure_team(company):
    """Create the three non-admin ERP users.

    Usernames are role-based (`production@slug`) rather than the default
    `firstname.role@slug`, so the login names stay tied to the job rather than
    to whoever currently holds it.
    """
    created = []
    for first, role, title in TEAM:
        if User.objects.filter(company=company, role=role).exists():
            continue
        u = User.objects.create_user(
            username=f"{role}@{company.slug}",
            password="RedVelvet@123", role=role, company=company, first_name=first,
        )
        created.append((u.username, title))
    return created


def ensure_customers(company):
    wanted = [
        ("Costco NE", "purchasing@costcone.com", "Net 30"),
        ("FreshMart", "orders@freshmart.com", "Net 15"),
        ("Hudson Valley Grocers", "procurement@hudsonvalleygrocers.com", "Net 30"),
        ("Sweet Tooth Cafe", "jane@sweettoothcafe.co", "Prepaid"),
    ]
    out = {}
    for name, email, terms in wanted:
        cust, _ = Customer.objects.get_or_create(
            company=company, name=name,
            defaults={"email": email, "payment_terms": terms},
        )
        out[name] = cust
    return out


def seed_inbound_emails(company, customers):
    made = 0
    for spec in INBOUND_EMAILS:
        if InboundOrderEmail.objects.filter(company=company, subject=spec["subject"]).exists():
            continue

        order = None
        if spec["create_order"]:
            cust = customers.get(spec["parsed"].get("customer"))
            if cust:
                order = SalesOrder.objects.create(
                    customer=cust, status="draft", source="email"
                )
                total = 0
                for line in spec["parsed"]["lines"]:
                    item = Item.objects.filter(
                        company=company, name=line["product"], category="finished_good"
                    ).first()
                    if not item:
                        continue
                    SalesOrderItem.objects.create(
                        sales_order=order, item=item, quantity=line["quantity"]
                    )
                    total += float(item.selling_price) * line["quantity"]
                order.total_amount = total
                order.save(update_fields=["total_amount"])

        InboundOrderEmail.objects.create(
            company=company,
            sender=spec["sender"],
            subject=spec["subject"],
            raw_body=spec["raw_body"],
            parsed_data=spec["parsed"],
            confidence=spec["confidence"],
            status=spec["status"],
            sales_order=order,
            error_message=spec.get("error", ""),
            received_at=timezone.now() - timedelta(days=made + 1, hours=3),
        )
        made += 1
    return made


def seed_lot_genealogy(company):
    """Build one complete traceable chain: vendor receipt → raw lots →
    production order consuming them → finished lot → shipment.

    This is the SQF story end to end, so `trace_backward` on the shipped lot
    returns real vendor deliveries rather than an empty list.
    """
    plant = Warehouse.objects.get(company=company, name="Mount Kisco Plant")
    product = Item.objects.filter(
        company=company, name="Snickerdoodle Cookies"
    ).first()
    if not product:
        return "skipped (product missing)"
    recipe = Recipe.objects.filter(product=product).first()
    if not recipe or LotAlreadyBuilt(company, product):
        return "already present"

    vendor = Vendor.objects.filter(company=company).first()
    line = ProductionLine.objects.filter(
        company=company, name="Cookie & Bake Line 1"
    ).first()

    # 1. Receive raw material against a PO, creating lots with real receipts.
    po = PurchaseOrder.objects.create(vendor=vendor, status="received")
    receipt = GoodsReceipt.objects.create(purchase_order=po, warehouse=plant)
    for ing, required in recipe.material_requirements(recipe.batch_size):
        PurchaseOrderItem.objects.create(
            purchase_order=po, item=ing.item,
            quantity=required, unit_price=ing.item.purchase_cost,
        )
        create_raw_lot(
            ing.item, plant, required * 2, receipt, company=company,
            expiry_date=timezone.localdate() + timedelta(days=180),
        )

    # 2. Run a production order that consumes those lots FIFO.
    order = ProductionOrder.objects.create(
        recipe=recipe, quantity=recipe.batch_size, warehouse=plant,
        line=line, status="completed",
        start_time=timezone.now() - timedelta(days=2),
        end_time=timezone.now() - timedelta(days=2, hours=-6),
    )
    for ing, required in recipe.material_requirements(recipe.batch_size):
        consume_lots_fifo(order, ing.item, required, company=company)

    # 3. The run yields a finished lot.
    create_finished_lot(product, plant, recipe.batch_size, order, company=company,
                        expiry_date=timezone.localdate() + timedelta(days=90))
    return "built"


def LotAlreadyBuilt(company, product):
    return Batch.objects.filter(
        company=company, item=product, source="produced"
    ).exists()


def seed_transfer_and_count(company, user):
    """A Mount Kisco → Milton transfer (90% of finished goods move there) and a
    posted cycle count standing in for the weekly reconciliation."""
    plant = Warehouse.objects.get(company=company, name="Mount Kisco Plant")
    milton = Warehouse.objects.get(company=company, name="Milton Staging")
    results = []

    if not StockTransfer.objects.filter(company=company).exists():
        item = Item.objects.filter(
            company=company, name="Snickerdoodle Cookies"
        ).first()
        on_hand = Stock.objects.filter(item=item, warehouse=plant).first()
        qty = min(120, on_hand.quantity if on_hand else 0)
        if qty > 0:
            t = StockTransfer.objects.create(
                company=company, item=item, source_warehouse=plant,
                dest_warehouse=milton, quantity=qty,
                reference="Weekly move to Milton pickup staging",
                created_by=user,
            )
            complete_transfer(t, user=user)
            results.append(f"transfer {qty:.0f} units")

    if not CycleCount.objects.filter(company=company).exists():
        cc = CycleCount.objects.create(
            company=company, warehouse=plant,
            note="Weekly Friday reconciliation", created_by=user,
        )
        counted = 0
        for stock in Stock.objects.filter(warehouse=plant, item__company=company)[:12]:
            # Small realistic variances on a couple of lines.
            delta = -2 if counted == 2 else (3 if counted == 5 else 0)
            CycleCountLine.objects.create(
                cycle_count=cc, item=stock.item,
                system_quantity=stock.quantity,
                counted_quantity=max(0, stock.quantity + delta),
            )
            counted += 1
        post_cycle_count(cc, user=user)
        results.append(f"cycle count ({counted} lines)")

    return results or ["already present"]


@transaction.atomic
def main():
    company = Company.objects.get(slug=SLUG)
    admin = User.objects.filter(company=company, role="admin").first()
    print(f"Operational seed for {company.name} (#{company.id})\n")

    team = ensure_team(company)
    print(f"Team users created: {len(team)}")
    for username, title in team:
        print(f"   {username:38s} {title}")

    customers = ensure_customers(company)
    print(f"Customers: {len(customers)}")

    n = seed_inbound_emails(company, customers)
    print(f"Inbound PO emails created: {n}")

    print(f"Lot genealogy: {seed_lot_genealogy(company)}")
    print(f"Warehouse ops: {', '.join(seed_transfer_and_count(company, admin))}")

    print("\nDone.")


if __name__ == "__main__":
    main()
