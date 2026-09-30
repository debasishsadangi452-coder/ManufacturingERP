"""Add the aluminium welding-wire raw materials (metals, consumables and
packaging) to an existing company's catalogue.

    python manage.py seed_welding_wire_materials --company <slug>
    python manage.py seed_welding_wire_materials --user <admin username>
    python manage.py seed_welding_wire_materials --company <slug> --dry-run

Items are created with zero stock; stock arrives through purchase orders and
goods receipts as usual. Re-runnable: a material whose name already exists in
the company is left as it is, never duplicated or overwritten.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Company, User
from inventory.models import Item, UnitOfMeasure

# (name, unit, uom code, purchase cost per unit, sku)
RAW_MATERIALS = [
    ("Aluminum Ingot P1020", "lb", "lb", "1.30", "RM-AL-P1020"),
    ("Silicon Metal 553", "lb", "lb", "1.60", "RM-SI-553"),
    ("Magnesium Ingot 99.9%", "lb", "lb", "2.80", "RM-MG-999"),
    ("Wire Drawing Lubricant", "gal", None, "18.00", "RM-LUBE"),
    ("Plastic Spool 16 lb", "each", "each", "1.10", "PK-SPOOL-16"),
    ("Metal Basket 20 lb", "each", "each", "2.40", "PK-BASKET-20"),
    ("Payoff Drum 500 lb", "each", "each", "55.00", "PK-DRUM-500"),
    ("Wooden Reel 300 lb", "each", "each", "22.00", "PK-REEL-300"),
    ("TIG Rod Tube 10 lb", "each", "each", "1.80", "PK-TUBE-10"),
    ("Shipping Carton", "each", "each", "0.60", "PK-CARTON"),
]


class Command(BaseCommand):
    help = "Add aluminium welding-wire raw materials to an existing company."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--company", help="Company slug.")
        target.add_argument("--user", help="Username of any user in the company, e.g. the admin.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be created without saving.")

    def _company(self, options):
        if options["company"]:
            company = Company.objects.filter(slug=options["company"]).first()
            if not company:
                raise CommandError(f"No company with slug '{options['company']}'.")
            return company
        user = User.objects.filter(username=options["user"]).select_related("company").first()
        if not user or not user.company:
            raise CommandError(f"No user '{options['user']}' belonging to a company.")
        return user.company

    @transaction.atomic
    def handle(self, *args, **options):
        company = self._company(options)
        dry_run = options["dry_run"]
        existing = set(Item.objects.filter(company=company).values_list("name", flat=True))
        uoms = {u.code: u for u in UnitOfMeasure.objects.filter(company=None)}

        created, skipped = [], []
        for name, unit, uom_code, cost, sku in RAW_MATERIALS:
            if name in existing:
                skipped.append(name)
                continue
            created.append(name)
            if dry_run:
                continue
            uom = uoms.get(uom_code) if uom_code else None
            Item.objects.create(
                company=company, name=name, category="raw_material", erp_classification="raw_material",
                unit=unit, sku=sku,
                base_unit=uom, purchase_unit=uom, purchase_cost=Decimal(cost),
            )

        verb = "Would create" if dry_run else "Created"
        self.stdout.write(self.style.SUCCESS(f"{company.name}: {verb} {len(created)} raw material(s)"))
        for name in created:
            self.stdout.write(f"  + {name}")
        if skipped:
            self.stdout.write(f"Already present, left unchanged: {len(skipped)}")
            for name in skipped:
                self.stdout.write(f"  = {name}")
