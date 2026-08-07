"""Seed demo order emails for the 'Orders from Email' inbox.

Creates a realistic spread across every status so the review screen, the Sent
tab and the delete controls all have something to show. Confirmed entries are
linked to a real SalesOrder, because that is what the Sent tab reads.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Company
from inventory.models import Item
from sales.models import Customer, InboundOrderEmail, SalesOrder, SalesOrderItem

SUBJECTS = [
    "PO #{n} — weekly replenishment",
    "Purchase order {n} for next week",
    "Re: standing order — week {n}",
    "New order {n}, please confirm",
    "Order request #{n} (urgent)",
    "{n} — restock request",
    "Wholesale order {n}",
    "PO{n} attached — please acknowledge",
]

BODY = (
    "Hi,\n\nPlease process the following order for delivery to our main store.\n\n"
    "{lines}\n\nPlease confirm receipt and expected despatch date.\n\n"
    "Kind regards,\n{contact}\n{customer}"
)

CONTACTS = ["Sarah Whitfield", "Danny Alvarez", "Priya Raman", "Tom Beckett",
            "Ana Sousa", "Marcus Lin", "Hannah Ford", "Ravi Menon"]


class Command(BaseCommand):
    help = "Seed demo inbound order emails for the Orders from Email inbox."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=18,
                            help="How many emails to create (default 18).")
        parser.add_argument("--company", type=str, default=None,
                            help="Company name to seed into. Defaults to the first company.")
        parser.add_argument("--clear", action="store_true",
                            help="Delete existing unconfirmed inbound emails first.")

    def handle(self, *args, **options):
        count = options["count"]

        if options["company"]:
            company = Company.objects.filter(name=options["company"]).first()
            if not company:
                self.stderr.write(f"No company named '{options['company']}'.")
                return
        else:
            # Prefer a company that already has customers and sellable items —
            # seeding into an empty tenant would produce unusable rows.
            usable = (
                set(Customer.objects.values_list("company_id", flat=True))
                & set(Item.objects.filter(category="finished_good")
                      .values_list("company_id", flat=True))
            )
            usable.discard(None)
            company = (
                Company.objects.filter(id__in=usable).order_by("id").first()
                or Company.objects.first()
            )
        if not company:
            self.stderr.write("No companies exist — nothing to seed.")
            return

        customers = list(Customer.objects.filter(company=company))
        products = list(Item.objects.filter(company=company, category="finished_good"))
        if not customers or not products:
            self.stderr.write(
                f"'{company.name}' needs at least one customer and one finished good. "
                f"Found {len(customers)} customer(s), {len(products)} product(s)."
            )
            return

        if options["clear"]:
            removed, _ = InboundOrderEmail.objects.filter(
                company=company).exclude(status="confirmed").delete()
            self.stdout.write(f"Cleared {removed} unconfirmed email(s).")

        # A believable inbox: mostly reviewable drafts, a few already sent, and
        # a couple needing attention.
        plan = (
            ["parsed"] * max(1, int(count * 0.45))
            + ["confirmed"] * max(1, int(count * 0.30))
            + ["needs_attention"] * max(1, int(count * 0.15))
            + ["received"] * max(1, int(count * 0.10))
        )
        plan = (plan * ((count // len(plan)) + 1))[:count]
        random.shuffle(plan)

        now = timezone.now()
        created = 0

        for i, state in enumerate(plan):
            customer = random.choice(customers)
            chosen = random.sample(products, k=min(len(products), random.randint(1, 3)))
            po_no = 4200 + i
            received_at = now - timedelta(hours=random.randint(1, 400))

            lines_text, lines = [], []
            for prod in chosen:
                cases = random.choice([5, 10, 12, 20, 24, 30, 48, 50])
                lines_text.append(f"  - {cases} cases {prod.name}")
                lines.append((prod, cases))

            sales_order = None
            # Only a confirmed email has a real order behind it; that link is
            # what the Sent tab keys on.
            if state == "confirmed":
                sales_order = SalesOrder.objects.create(
                    customer=customer, status="pending",
                    total_amount=sum(
                        (p.selling_price or 0) * c for p, c in lines
                    ),
                )
                for prod, cases in lines:
                    # SalesOrderItem carries no price — the total is derived
                    # from the item's selling_price on the order itself.
                    SalesOrderItem.objects.create(
                        sales_order=sales_order, item=prod, quantity=cases,
                    )

            confidence = {
                "parsed": round(random.uniform(0.82, 0.97), 2),
                "confirmed": round(random.uniform(0.88, 0.99), 2),
                "needs_attention": round(random.uniform(0.31, 0.58), 2),
                "received": None,
            }[state]

            contact = random.choice(CONTACTS)
            domain = customer.name.lower().replace(" ", "").replace(",", "")[:18]

            InboundOrderEmail.objects.create(
                company=company,
                sender=f"{contact.split()[0].lower()}@{domain}.com",
                subject=random.choice(SUBJECTS).format(n=po_no),
                received_at=received_at,
                raw_body=BODY.format(
                    lines="\n".join(lines_text), contact=contact, customer=customer.name
                ),
                parsed_data={
                    "customer": customer.name,
                    "po_number": f"PO-{po_no}",
                    "lines": [{"product": p.name, "cases": c} for p, c in lines],
                },
                confidence=confidence,
                status=state,
                sales_order=sales_order,
                error_message=(
                    "Could not match customer with confidence — please verify."
                    if state == "needs_attention" else ""
                ),
            )
            created += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Created {created} inbound order email(s) for '{company.name}'."
        ))
        for state in ("parsed", "confirmed", "needs_attention", "received"):
            n = InboundOrderEmail.objects.filter(company=company, status=state).count()
            self.stdout.write(f"  {state:16} {n}")
