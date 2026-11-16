"""Seed a demo aluminium welding-wire manufacturer under any company name, with
a month of history: users, plant, production lines, raw materials, finished
goods, BOMs/recipes, vendors and customers, received and open purchase orders,
completed production with QC, shipped/invoiced/paid and open sales orders,
vendor bills and payments, and ERP-only journals (capital and operating expenses).

    python manage.py seed_welding_wire_demo --company "Dummy Company"
    python manage.py seed_welding_wire_demo --company "Dummy Company" --slug dummycompany
        --admin-username admin@dummycompany --password demo12345 [--brand Acme] [--replace]

--slug defaults to the company name without spaces or dashes, lowercased;
--admin-username defaults to admin@<slug>. The other users are named
firstname.role@<slug>. --brand, when given, prefixes every finished-good name.

All history is created through the same business functions the app uses and
posted to the ERP ledger by GL auto-posting, so stock, lots, bills, invoices,
payments and reports agree.

The company is built to be QuickBooks-ready:
  * items are already classified and every finished good has an active BOM, so
    the QuickBooks setup wizard has nothing to classify; it starts at customer
    mapping (the mapping and the sales / purchasing choices are left for a
    person to confirm);
  * customers and vendors carry the contact details QuickBooks receives, and no
    name is used twice (QuickBooks keeps customers and vendors in one name list);
  * the history pushes to a QuickBooks company without a single sync error.
Nothing is sent to QuickBooks by this command and no connection is created:
connect in Accounting > QuickBooks, finish the wizard, then "Send everything to
QuickBooks".

It also uses the manufacturing set-up: manufacturing settings, machines and
operators, a routing for every product, one confirmed customer order with a
production plan and the purchase requisition MRP raised for its shortages, and
one delivery sent through Fulfilment (pick, pack, dispatch, invoice).

If a company with the slug already exists the command stops, unless --replace
is given: --replace DELETES that company and everything in it first. A company
connected to QuickBooks is refused even then (deleting it would drop the link
and leave its records behind in QuickBooks) unless --force-replace-quickbooks.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from accounting.auto_posting import execute_auto_post
from accounting.engine import post_journal_entry
from accounting.models import Account, JournalEntry, JournalEntryLine
from accounting.payables import record_and_allocate_ap_payment
from accounting.seeds import seed_standard_chart_of_accounts, seed_standard_fiscal_year
from accounts.models import Company, CompanySubscription, User, generate_username
from inventory.lots import consume_lots_fifo, create_finished_lot, create_raw_lot, ship_lots_fifo
from fulfillment import services as fulfilment
from inventory.models import (
    BOM, BOMLine, Batch, Item, ProcurementQuickBooksConfig, QuickBooksOnboarding, SalesQuickBooksConfig,
    UnitOfMeasure, Warehouse,
)
from inventory.services import decrease_stock, increase_stock
from procurement.billing import create_bill_from_po
from procurement.models import (
    GoodsReceipt, PurchaseOrder, PurchaseOrderItem, PurchaseRequisition, PurchaseRequisitionItem, Vendor,
    VendorPriceList,
)
from production.models import (
    ManufacturingSettings, ProductionLine, ProductionOrder, ProductionPlan, Recipe, RecipeIngredient, Resource,
    RoutingStep,
)
from production.mrp import calculate_mrp_for_plan
from quickbooks.models import QuickBooksConnection
from quality.models import QualityCheck
from sales.models import (
    Customer, CustomerPayment, Invoice, InvoiceLine, SalesOrder, SalesOrderItem, Shipment, ShipmentLot,
)

D = Decimal


class Command(BaseCommand):
    help = "Seed an aluminium welding-wire demo company with a month of operating and accounting history."
    # Quality check recorded on each completed batch: (test type, parameter, result, target, remarks)
    QC = ("Tensile & chemistry", "Si/Mg content, wire diameter", "Within AWS A5.10 spec", "AWS A5.10", "Released for sale")

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help='Company name, e.g. "Dummy Company".')
        parser.add_argument("--slug", default="", help="Company slug (default: from the name).")
        parser.add_argument("--admin-username", default="", help="Admin login (default: admin@<slug>).")
        parser.add_argument("--password", default="demo12345", help="Password for every demo user.")
        parser.add_argument("--brand", default="", help="Optional brand prefixed to finished-good names.")
        parser.add_argument("--sku-prefix", default="WW", help="Prefix for finished-good SKUs.")
        parser.add_argument("--replace", action="store_true",
                            help="Delete an existing company with this slug (and all its data) first.")
        parser.add_argument("--force-replace-quickbooks", action="store_true",
                            help="Allow --replace on a company that is connected to QuickBooks.")

    @transaction.atomic
    def handle(self, *args, **options):
        self.today = timezone.localdate()
        self.year_start = date(self.today.year, 1, 1)
        self.company_name = options["company"].strip()
        self.slug = (options["slug"].strip() or slugify(self.company_name).replace("-", "")).lower()
        if not self.company_name or not self.slug:
            raise CommandError("A company name is required.")
        self.admin_username = options["admin_username"].strip() or f"admin@{self.slug}"
        self.brand = options["brand"].strip()
        self.sku_prefix = options["sku_prefix"].strip() or "WW"

        existing = Company.objects.filter(slug=self.slug).first()
        if existing and not options["replace"]:
            raise CommandError(
                f"Company '{existing.name}' (slug '{self.slug}') already exists. "
                "Use --replace to delete it and all its data, or pick another --slug."
            )
        if existing:
            if QuickBooksConnection.objects.filter(company=existing).exists() and not options["force_replace_quickbooks"]:
                raise CommandError(
                    f"'{existing.name}' is connected to QuickBooks. Replacing it deletes the connection and its sync "
                    "history and leaves every record already sent behind in QuickBooks, so the new company would "
                    "push duplicates. Disconnect and clean up QuickBooks first, or pass --force-replace-quickbooks."
                )
            self._teardown(existing)
        if Company.objects.filter(name=self.company_name).exists():
            raise CommandError(f"Another company is already named '{self.company_name}'; pick a different --company.")
        if User.objects.filter(username=self.admin_username).exists():
            raise CommandError(f"The username '{self.admin_username}' is already taken; pass --admin-username.")

        company = self.company = Company.objects.create(name=self.company_name, slug=self.slug)
        CompanySubscription.objects.create(company=company, plan="premium_ai", status="active", onboarding_completed=True)
        self.users = self._users(options["password"])
        self.admin = self.users["admin"]

        seed_standard_chart_of_accounts(company)
        seed_standard_fiscal_year(company, self.today.year)
        self._manufacturing_settings()

        self.build_history()
        self._stage_quickbooks()
        self._report(options["password"])

    def build_history(self):
        """Plant, catalogue and a month of operating history. Subclasses seeding
        a different business override this and reuse the helpers below."""
        company = self.company
        plant = Warehouse.objects.create(company=company, name="Main Plant - Casting & Drawing", location="Plant floor")
        fg = Warehouse.objects.create(company=company, name="Finished Goods Warehouse", location="Shipping dock")
        lines = {
            "drawing": ProductionLine.objects.create(company=company, name="Rod Casting & Wire Drawing", capacity=400),
            "spool": ProductionLine.objects.create(company=company, name="MIG Spooling & Packaging", capacity=250),
            "tig": ProductionLine.objects.create(company=company, name="TIG Rod Cutting", capacity=150),
        }

        rm = self._raw_materials()
        fgs = self._finished_goods()
        recipes = self._recipes_and_boms(rm, fgs)
        line_for = {"er4043_spool": "spool", "er5356_spool": "spool", "er4043_basket": "spool",
                    "er5356_tig": "tig", "er4043_drum": "drawing", "er5356_reel": "drawing"}
        for key, recipe in recipes.items():
            recipe.default_line = lines[line_for[key]]
            recipe.save(update_fields=["default_line"])
        self._routing(recipes, lines)
        vendors = self._vendors(rm)
        customers = self._customers()

        # --- Purchasing history -------------------------------------------------
        po_ingot = self._po(vendors["ingot"], [(rm["ingot"], 20000, "1.30")], "ordered", 28)
        po_alloy = self._po(vendors["alloy"], [(rm["silicon"], 1000, "1.60"), (rm["magnesium"], 800, "2.80")], "ordered", 28)
        po_pack = self._po(vendors["pack"], [
            (rm["spool"], 1000, "1.10"), (rm["basket"], 400, "2.40"), (rm["drum"], 20, "55.00"),
            (rm["reel"], 30, "22.00"), (rm["tube"], 800, "1.80"), (rm["carton"], 2000, "0.60"),
        ], "ordered", 27)
        po_lube = self._po(vendors["lube"], [(rm["lube"], 60, "18.00")], "ordered", 27)
        bills = {}
        for key, po, days in [("ingot", po_ingot, 25), ("alloy", po_alloy, 25), ("pack", po_pack, 24), ("lube", po_lube, 24)]:
            bills[key] = self._receive(po, plant, days)
        self._pay_bill(bills["ingot"], bills["ingot"].total_amount, 10, "ACH-10231")
        self._pay_bill(bills["alloy"], D("2000.00"), 5, "CHQ-5512")
        # Open purchase orders (not yet received)
        self._po(vendors["ingot"], [(rm["ingot"], 10000, "1.30")], "ordered", 3)
        self._po(vendors["pack"], [(rm["spool"], 1000, "1.10"), (rm["carton"], 1000, "0.60")], "draft", 1)

        # --- Production history -------------------------------------------------
        produced = [
            ("er4043_spool", 100, lines["spool"], 20),
            ("er5356_spool", 100, lines["spool"], 19),
            ("er4043_basket", 80, lines["spool"], 18),
            ("er5356_tig", 160, lines["tig"], 17),
            ("er4043_drum", 4, lines["drawing"], 16),
            ("er5356_reel", 6, lines["drawing"], 15),
        ]
        for key, qty, line, days in produced:
            self._produce(recipes[key], qty, line, plant, fg, days)
        # Scheduled (not started) batch
        ProductionOrder.objects.create(
            recipe=recipes["er4043_spool"], quantity=100, warehouse=fg, line=lines["spool"],
            status="scheduled", start_time=self._at(-2),
        )

        # --- Sales history ----------------------------------------------------------
        so1 = self._sell(customers["atlantic"], [(fgs["er5356_spool"], 40)], fg, 14)
        so2 = self._sell(customers["keystone"], [(fgs["er4043_spool"], 30), (fgs["er4043_basket"], 20)], fg, 12)
        so3 = self._sell(customers["summit"], [(fgs["er5356_tig"], 60)], fg, 9)
        so4 = self._sell(customers["lakeside"], [(fgs["er4043_drum"], 2), (fgs["er5356_reel"], 2)], fg, 7)
        inv1 = self._invoice(so1, 14)
        inv2 = self._invoice(so2, 12)
        self._invoice(so3, 9)
        self._invoice(so4, 7)
        self._customer_payment(inv1, inv1.total_amount, 3, "WIRE-88213")
        self._customer_payment(inv2, D("3000.00"), 2, "CHQ-40117")
        # Open sales orders
        self._order(customers["harbor"], [(fgs["er5356_spool"], 30)], "pending", 2)
        # One delivery goes out through Fulfilment: allocate, pick, pack, stage, dispatch, invoice.
        self._dispatch_through_fulfilment(customers["keystone"], [(fgs["er4043_basket"], 30)])
        # One large confirmed order that stock cannot cover: production plan, MRP, requisition.
        self._make_to_order_chain(customers["atlantic"], fgs["er5356_spool"], 1500, plant)

        # --- ERP-only accounting (sent to QuickBooks as journal entries) --------------
        self._journal(30, "Owner capital contribution", [("1010", "250000.00", "0"), ("3010", "0", "250000.00")])
        self._journal(29, "September plant rent", [("6050", "6500.00", "0"), ("1010", "0", "6500.00")])
        self._journal(8, "Outbound freight - LTL carriers", [("6030", "1250.00", "0"), ("1010", "0", "1250.00")])
        self._journal(6, "Plant electricity - wire drawing furnaces", [("5200", "4800.00", "0"), ("1010", "0", "4800.00")])
        self._journal(1, "Admin & sales salaries", [("6010", "12000.00", "0"), ("1010", "0", "12000.00")])

    # ------------------------------------------------------------------ helpers

    def _teardown(self, old):
        ShipmentLot.objects.filter(lot__company=old).delete()
        Batch.objects.filter(company=old).delete()
        JournalEntryLine.objects.filter(company=old).delete()
        JournalEntry.objects.filter(company=old).update(reversal_of=None)
        JournalEntry.objects.filter(company=old).delete()
        old.delete()

    def _users(self, password):
        people = [("Maya", "admin"), ("Victor", "production"), ("Sam", "store"),
                  ("Priya", "sales"), ("Omar", "finance"), ("Lena", "quality")]
        users = {}
        for first, role in people:
            users[role] = User.objects.create_user(
                username=generate_username(first, role, self.company), password=password, role=role,
                company=self.company, first_name=first, last_name=self.company_name[:150], is_staff=(role == "admin"),
            )
        users["admin"].username = self.admin_username
        users["admin"].save(update_fields=["username"])
        return users

    def _day(self, days_ago):
        """A date `days_ago` before today, never before 1 Jan of this year (open fiscal year)."""
        return max(self.today - timedelta(days=days_ago), self.year_start)

    def _at(self, days_ago, hour=10):
        return timezone.make_aware(datetime.combine(self._day(days_ago), time(hour)))

    def _uom(self, code):
        return UnitOfMeasure.objects.filter(company=None, code=code).first()

    def _item(self, name, category, unit, uom=None, cost="0", price="0", sku=""):
        return Item.objects.create(
            company=self.company, name=name, category=category, unit=unit, sku=sku,
            base_unit=self._uom(uom) if uom else None, purchase_unit=self._uom(uom) if uom else None,
            purchase_cost=D(cost), selling_price=D(price), erp_classification=category,
            classification_completed_at=timezone.now(),
        )

    def _raw_material_items(self):
        return {
            "ingot": self._item("Aluminum Ingot P1020", "raw_material", "lb", "lb", "1.30", sku="RM-AL-P1020"),
            "silicon": self._item("Silicon Metal 553", "raw_material", "lb", "lb", "1.60", sku="RM-SI-553"),
            "magnesium": self._item("Magnesium Ingot 99.9%", "raw_material", "lb", "lb", "2.80", sku="RM-MG-999"),
            "lube": self._item("Wire Drawing Lubricant", "raw_material", "gal", None, "18.00", sku="RM-LUBE"),
            "spool": self._item("Plastic Spool 16 lb", "raw_material", "each", "each", "1.10", sku="PK-SPOOL-16"),
            "basket": self._item("Metal Basket 20 lb", "raw_material", "each", "each", "2.40", sku="PK-BASKET-20"),
            "drum": self._item("Payoff Drum 500 lb", "raw_material", "each", "each", "55.00", sku="PK-DRUM-500"),
            "reel": self._item("Wooden Reel 300 lb", "raw_material", "each", "each", "22.00", sku="PK-REEL-300"),
            "tube": self._item("TIG Rod Tube 10 lb", "raw_material", "each", "each", "1.80", sku="PK-TUBE-10"),
            "carton": self._item("Shipping Carton", "raw_material", "each", "each", "0.60", sku="PK-CARTON"),
        }

    # (reorder point, safety stock, minimum order quantity, lead time in days) per raw material
    PLANNING = {
        "ingot": (8000, 3000, 5000, 7), "silicon": (300, 100, 500, 10), "magnesium": (250, 100, 400, 10),
        "lube": (20, 10, 10, 3), "spool": (400, 150, 100, 14), "basket": (150, 50, 100, 14),
        "drum": (8, 4, 5, 14), "reel": (10, 5, 5, 14), "tube": (300, 100, 100, 14), "carton": (800, 300, 100, 14),
    }

    def _raw_materials(self):
        """Raw materials with the planning data MRP and the reorder alerts read."""
        rm = self._raw_material_items()
        for key, (reorder, safety, moq, lead) in self.PLANNING.items():
            item = rm[key]
            item.reorder_point, item.safety_stock = reorder, safety
            item.minimum_order_quantity, item.lead_time_days = moq, lead
            item.save(update_fields=["reorder_point", "safety_stock", "minimum_order_quantity", "lead_time_days"])
        return rm

    def _finished_goods(self):
        brand = f"{self.brand} " if self.brand else ""

        def fg(name, unit, price, sku):
            return self._item(brand + name, "finished_good", unit, "each", "0", price, f"{self.sku_prefix}-{sku}")

        return {
            "er4043_spool": fg("ER4043 MIG Wire 0.035in - 16 lb Spool", "spool", "98.00", "4043-035-S16"),
            "er5356_spool": fg("ER5356 MIG Wire 0.035in - 16 lb Spool", "spool", "105.00", "5356-035-S16"),
            "er4043_basket": fg("ER4043 MIG Wire 3/64in - 20 lb Basket", "basket", "128.00", "4043-364-B20"),
            "er5356_tig": fg("ER5356 TIG Rod 1/16in - 10 lb Tube", "tube", "72.00", "5356-116-T10"),
            "er4043_drum": fg("ER4043 MIG Wire 3/64in - 500 lb Drum", "drum", "2650.00", "4043-364-D500"),
            "er5356_reel": fg("ER5356 MIG Wire 1/16in - 300 lb Wooden Reel", "reel", "1650.00", "5356-116-R300"),
        }

    def _recipes_and_boms(self, rm, fgs):
        # (finished good, batch size in units, [(raw material, quantity per batch)])
        specs = {
            "er4043_spool": (50, [("ingot", 760), ("silicon", 42), ("lube", 2), ("spool", 50), ("carton", 50)]),
            "er5356_spool": (50, [("ingot", 760), ("magnesium", 40), ("lube", 2), ("spool", 50), ("carton", 50)]),
            "er4043_basket": (40, [("ingot", 760), ("silicon", 42), ("lube", 2), ("basket", 40), ("carton", 40)]),
            "er5356_tig": (80, [("ingot", 760), ("magnesium", 40), ("lube", 1), ("tube", 80), ("carton", 80)]),
            "er4043_drum": (2, [("ingot", 950), ("silicon", 52), ("lube", 3), ("drum", 2)]),
            "er5356_reel": (3, [("ingot", 855), ("magnesium", 45), ("lube", 2), ("reel", 3)]),
        }
        recipes = {}
        for key, (batch_size, parts) in specs.items():
            recipe = Recipe.objects.create(product=fgs[key], batch_size=batch_size)
            bom = BOM.objects.create(finished_good=fgs[key], version="1.0", status="active", is_active=True)
            for rm_key, qty in parts:
                RecipeIngredient.objects.create(recipe=recipe, item=rm[rm_key], quantity=qty)
                BOMLine.objects.create(
                    bom=bom, raw_material=rm[rm_key], quantity=round(qty / batch_size, 4),
                    unit=rm[rm_key].unit, unit_of_measure=rm[rm_key].base_unit,
                )
            recipes[key] = recipe
        return recipes

    def _vendors(self, rm):
        vendors = {
            "ingot": Vendor.objects.create(company=self.company, name="Great Lakes Aluminum Supply",
                                           email="orders@greatlakes-al.example", payment_terms="Net 30"),
            "alloy": Vendor.objects.create(company=self.company, name="Midwest Alloying Metals",
                                           email="sales@midwestalloy.example", payment_terms="Net 45"),
            "pack": Vendor.objects.create(company=self.company, name="Packwell Industrial Packaging",
                                          email="orders@packwell.example", payment_terms="Net 30"),
            "lube": Vendor.objects.create(company=self.company, name="DrawTech Lubricants",
                                          email="ar@drawtech.example", payment_terms="Due on receipt"),
        }
        prices = [("ingot", "ingot", "1.30", 5000, 7), ("alloy", "silicon", "1.60", 500, 10),
                  ("alloy", "magnesium", "2.80", 400, 10), ("lube", "lube", "18.00", 10, 3)]
        prices += [("pack", k, None, 100, 14) for k in ("spool", "basket", "drum", "reel", "tube", "carton")]
        for v_key, rm_key, price, moq, lead in prices:
            VendorPriceList.objects.create(vendor=vendors[v_key], item=rm[rm_key],
                                           unit_price=D(price) if price else rm[rm_key].purchase_cost,
                                           min_order_qty=moq, lead_time_days=lead)
        # Contact details travel to QuickBooks with the vendor record.
        contacts = {
            "ingot": ("+1-312-555-0141", "2200 Lake Shore Dr, Chicago, IL 60616", "36-7710231", 4.6),
            "alloy": ("+1-414-555-0188", "815 W Canal St, Milwaukee, WI 53233", "39-2204417", 4.4),
            "pack": ("+1-216-555-0123", "4400 Euclid Ave, Cleveland, OH 44103", "34-1609925", 4.3),
            "lube": ("+1-313-555-0167", "1750 Fort St, Detroit, MI 48226", "38-5521160", 4.5),
        }
        for key, (phone, address, tax_id, rating) in contacts.items():
            Vendor.objects.filter(pk=vendors[key].pk).update(phone=phone, address=address, tax_id=tax_id, rating=rating)
            vendors[key].refresh_from_db()
        return vendors

    def _customers(self):
        spec = {
            "atlantic": ("Atlantic Marine Fabricators", "purchasing@atlanticmarine.example", "Net 30"),
            "keystone": ("Keystone Trailer Works", "ap@keystonetrailer.example", "Net 30"),
            "summit": ("Summit Aerospace Components", "procurement@summitaero.example", "Net 45"),
            "lakeside": ("Lakeside Welding Supply", "orders@lakesideweld.example", "Due on receipt"),
            "harbor": ("Harbor Boat Builders", "buying@harborboats.example", "Net 30"),
        }
        contacts = {
            "atlantic": ("+1-757-555-0102", "310 Harbor Way, Norfolk, VA 23510"),
            "keystone": ("+1-717-555-0175", "88 Industrial Pkwy, Lancaster, PA 17601"),
            "summit": ("+1-206-555-0134", "9100 Aerospace Blvd, Everett, WA 98204"),
            "lakeside": ("+1-216-555-0190", "1420 Lakeside Ave, Cleveland, OH 44114"),
            "harbor": ("+1-401-555-0156", "27 Wharf St, Newport, RI 02840"),
        }
        return {k: Customer.objects.create(company=self.company, name=n, email=e, payment_terms=t,
                                           phone=contacts[k][0], address=contacts[k][1])
                for k, (n, e, t) in spec.items()}

    def _po(self, vendor, rows, status, days_ago):
        po = PurchaseOrder.objects.create(vendor=vendor, status=status, priority="normal",
                                          expected_delivery=self._day(days_ago) + timedelta(days=7))
        for item, qty, price in rows:
            PurchaseOrderItem.objects.create(purchase_order=po, item=item, quantity=qty, unit_price=D(price))
        PurchaseOrder.objects.filter(pk=po.pk).update(created_at=self._at(days_ago, 9))
        po.refresh_from_db()
        return po

    def _receive(self, po, warehouse, days_ago):
        """Goods receipt → stock + raw lots, auto-posted receipt, vendor bill (auto-posted)."""
        receipt = GoodsReceipt.objects.create(purchase_order=po, warehouse=warehouse)
        GoodsReceipt.objects.filter(pk=receipt.pk).update(received_at=self._at(days_ago, 14))
        receipt.refresh_from_db()
        for poi in po.items.all():
            increase_stock(poi.item, warehouse, poi.quantity, user=self.users["store"], reference=f"GRN PO#{po.id}")
            create_raw_lot(poi.item, warehouse, poi.quantity, receipt, company=self.company)
        po.status = "received"
        po.save()
        self._post("goods_receipt", receipt.id, self.users["store"])
        bill = create_bill_from_po(po, bill_date=self._day(days_ago), bill_number=f"{po.vendor.name[:3].upper()}-{4100 + po.id}")
        self._post("vendor_bill", bill.id, self.users["store"])
        bill.refresh_from_db()
        return bill

    def _pay_bill(self, bill, amount, days_ago, reference):
        record_and_allocate_ap_payment(
            vendor_id=bill.vendor_id, amount=amount, user=self.users["finance"], company=self.company,
            payment_date=self._day(days_ago), method="bank_transfer", reference=reference,
            allocations=[{"bill_id": bill.id, "amount": str(amount)}],
        )

    def _produce(self, recipe, quantity, line, plant, fg, days_ago, qc=None):
        """Complete a production batch: consume raw stock/lots, add finished stock/lot, approve QC, post."""
        order = ProductionOrder.objects.create(recipe=recipe, quantity=quantity, warehouse=fg, line=line,
                                               status="running", start_time=self._at(days_ago, 7))
        for ingredient, required in recipe.material_requirements(quantity):
            decrease_stock(ingredient.item, plant, required, user=self.users["production"], reference=f"Production #{order.id}")
            consume_lots_fifo(order, ingredient.item, required, company=self.company)
        increase_stock(recipe.product, fg, quantity, user=self.users["production"], reference=f"Production #{order.id}")
        lot = create_finished_lot(recipe.product, fg, quantity, order, company=self.company)
        order.status = "completed"
        order.end_time = self._at(days_ago, 16)
        order.save()
        test_type, parameter, result, target, remarks = qc or self.QC
        QualityCheck.objects.create(production_order=order, lot=lot, status="approved", test_type=test_type,
                                    parameter=parameter, result=result, target=target, remarks=remarks)
        self._post("production_completed", order.id, self.users["production"])
        return order

    def _order(self, customer, rows, status, days_ago):
        order = SalesOrder.objects.create(customer=customer, status=status)
        total = D("0")
        for item, qty in rows:
            SalesOrderItem.objects.create(sales_order=order, item=item, quantity=qty)
            total += item.selling_price * D(str(qty))
        SalesOrder.objects.filter(pk=order.pk).update(total_amount=total, created_at=self._at(days_ago, 11))
        order.refresh_from_db()
        return order

    def _sell(self, customer, rows, fg, days_ago):
        """Sales order fulfilled from finished stock: shipment + lots, COGS auto-posted."""
        order = self._order(customer, rows, "confirmed", days_ago + 2)
        shipment = Shipment.objects.create(sales_order=order, warehouse=fg, status="delivered", progress=100,
                                           driver="Regional LTL")
        shipped = []
        for line in order.salesorderitem_set.select_related("item"):
            decrease_stock(line.item, fg, line.quantity, user=self.users["sales"], reference=f"Fulfilled SO#{order.id}")
            ship_lots_fifo(shipment, line.item, line.quantity, company=self.company)
            line.shipped_quantity = line.quantity
            line.save()
            shipped.append({"item_id": line.item_id, "quantity": line.quantity})
        order.status = "delivered"
        order.save()
        self._post("sales_shipment", order.id, self.users["sales"], {"lines": shipped})
        return order

    def _manufacturing_settings(self):
        """Make-to-order and make-to-stock both run here: wire is stocked and sold from stock,
        and large customer orders are planned. Raw material goes straight into stock."""
        settings = ManufacturingSettings.for_company(self.company)
        settings.business_model = "hybrid"
        settings.incoming_qc_required = False
        settings.enforce_operation_sequence = True
        settings.allow_partial_production_and_dispatch = True
        settings.auto_invoice_on_dispatch = True
        settings.save()

    # product -> process family
    FAMILY = {"er4043_spool": "mig", "er5356_spool": "mig", "er4043_basket": "mig",
              "er5356_tig": "tig", "er4043_drum": "bulk", "er5356_reel": "bulk"}

    def _routing(self, recipes, lines):
        """Machines, operators and a routing for every product (setup + run minutes per unit)."""
        def resource(name, kind, category, line, cost, rate=0):
            return Resource.objects.create(
                company=self.company, name=name, resource_type=kind, category=category, line=lines[line],
                cost_per_hour=D(cost), run_rate_per_hour=rate, status="available",
            )

        furnace = resource("Melting & Holding Furnace", "machine", "Furnace", "drawing", "140", 600)
        caster = resource("Continuous Rod Caster", "machine", "Caster", "drawing", "95", 500)
        drawer = resource("Wire Drawing Machine", "machine", "Drawing", "drawing", "85", 300)
        shaver = resource("Shaving & Cleaning Line", "machine", "Surface prep", "spool", "55", 250)
        spooler = resource("Precision Spooler", "machine", "Spooling", "spool", "60", 240)
        cutter = resource("TIG Straighten & Cut Line", "machine", "Cutting", "tig", "50", 200)
        winder = resource("Layer Winder", "machine", "Winding", "drawing", "45", 6)
        furnace_op = resource("Furnace Operator", "manpower", "Furnace operator", "drawing", "38")
        draw_op = resource("Drawing Operator", "manpower", "Drawing operator", "drawing", "34")
        spool_op = resource("Spooling Operator", "manpower", "Spooling operator", "spool", "30")
        qc = resource("QC Inspector", "manpower", "Quality inspector", "spool", "36")

        melt = ("Melt & cast rod", caster, furnace_op, 45, 0.50)
        draw = ("Draw to size", drawer, draw_op, 30, 0.80)
        routings = {
            "mig": [melt, draw, ("Shave & clean", shaver, draw_op, 15, 0.20),
                    ("Spool", spooler, spool_op, 10, 0.35), ("Inspect & pack", None, qc, 5, 0.15)],
            "tig": [melt, draw, ("Straighten & cut", cutter, spool_op, 15, 0.40),
                    ("Inspect & tube pack", None, qc, 5, 0.12)],
            "bulk": [melt, draw, ("Layer-wind", winder, draw_op, 20, 12.0), ("Inspect", None, qc, 10, 2.0)],
        }
        for key, recipe in recipes.items():
            for seq, (name, machine, manpower, setup, run) in enumerate(routings[self.FAMILY[key]], start=1):
                RoutingStep.objects.create(recipe=recipe, sequence=seq, name=name, machine=machine, manpower=manpower,
                                           setup_minutes=setup, run_minutes_per_unit=run)
        self.furnace = furnace  # kept for the report only

    def _dispatch_through_fulfilment(self, customer, rows):
        """A confirmed order shipped the way the Fulfilment page does it, invoice included."""
        store, sales = self.users["store"], self.users["sales"]
        order = self._order(customer, rows, "confirmed", 3)
        fulfilment.allocate(order, user=sales)
        dispatch = fulfilment.create_dispatch(order, user=store)
        fulfilment.pick(dispatch, user=store)
        fulfilment.pack(dispatch, packages=3, gross_weight="640 lb", user=store)
        fulfilment.stage(dispatch, user=store)
        fulfilment.prepare_shipment(dispatch, {
            "shipment_mode": "road", "carrier": "Regional LTL Freight", "vehicle_number": "TRK-2217",
            "driver_name": "D. Alvarez", "shipment_reference": "BOL-77219", "freight_amount": D("310.00"),
            "expected_delivery": self.today + timedelta(days=3),
        }, user=store)
        fulfilment.verify(dispatch, user=store)
        fulfilment.confirm_dispatch(dispatch, user=store, auto_invoice=True)
        dispatch.refresh_from_db()
        # Post now rather than when the command's transaction commits.
        self._post("dispatch_shipment", dispatch.id, store)
        self._post("sales_invoice", dispatch.invoice_id, sales)
        self.dispatch = dispatch
        return dispatch

    def _make_to_order_chain(self, customer, product, quantity, warehouse):
        """Confirmed customer order -> production plan -> MRP -> approved purchase requisition for the shortages."""
        order = self._order(customer, [(product, quantity)], "confirmed", 2)
        SalesOrder.objects.filter(pk=order.pk).update(
            required_delivery_date=self.today + timedelta(days=28), customer_order_reference="AMF-PO-7741",
            priority="high", source="email",
        )
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=order, sales_order_item=order.salesorderitem_set.first(),
            customer=customer, item=product, order_quantity=quantity, planned_quantity=quantity,
            target_date=self.today + timedelta(days=21), status="planned", created_by=self.admin,
            notes=f"Customer order AMF-PO-7741: {quantity:,} x {product.name}.",
        )
        shortages = [r for r in calculate_mrp_for_plan(plan)["raw_materials"] if r["shortage_quantity"] > 0]
        requisition = None
        if shortages:
            requisition = PurchaseRequisition.objects.create(
                company=self.company, production_plan=plan, sales_order=order, warehouse=warehouse,
                status="approved", created_by=self.admin,
                notes=f"MRP shortages for {plan.plan_number}; approved, ready to convert to purchase orders.",
            )
            costs = {i.id: i.purchase_cost for i in Item.objects.filter(pk__in=[r["item_id"] for r in shortages])}
            for row in shortages:
                PurchaseRequisitionItem.objects.create(
                    requisition=requisition, item_id=row["item_id"], required_quantity=row["required_quantity"],
                    available_quantity=row["available_quantity"], shortage_quantity=row["shortage_quantity"],
                    uom=row["unit"] or "", estimated_unit_price=costs.get(row["item_id"], D("0")),
                )
        self.plan, self.requisition = plan, requisition
        return plan

    def _stage_quickbooks(self):
        """Leave the QuickBooks setup wizard where it belongs for a company whose items and BOMs were
        created in the ERP: nothing to classify, BOMs done, so it opens at customer mapping. The mapping
        and the sales / purchasing choices stay unconfirmed; a person makes those decisions."""
        with_bom = BOM.objects.filter(finished_good__company=self.company, is_active=True)
        Item.objects.filter(pk__in=list(with_bom.values_list("finished_good_id", flat=True))).update(bom_completed=True)
        QuickBooksOnboarding.objects.update_or_create(company=self.company, defaults={"status": "customer_mapping"})
        SalesQuickBooksConfig.objects.update_or_create(company=self.company, defaults={
            "sale_trigger": "shipment", "default_doc_type": "invoice",
            "customers_mapped": False, "price_disclaimer_acknowledged": False,
        })
        ProcurementQuickBooksConfig.objects.update_or_create(company=self.company, defaults={
            "purchase_trigger": "goods_receipt", "cost_source": "po_price",
            "vendors_mapped": False, "payables_disclaimer_acknowledged": False,
        })

    def _terms_days(self, terms):
        digits = "".join(c for c in (terms or "") if c.isdigit())
        return int(digits) if digits else 0

    def _invoice(self, order, days_ago):
        invoice_date = self._day(days_ago)
        invoice = Invoice.objects.create(
            company=self.company, sales_order=order, customer=order.customer, invoice_date=invoice_date,
            due_date=invoice_date + timedelta(days=self._terms_days(order.customer.payment_terms)),
        )
        total = D("0")
        for line in order.salesorderitem_set.select_related("item"):
            amount = line.item.selling_price * D(str(line.quantity))
            InvoiceLine.objects.create(invoice=invoice, item=line.item, description=line.item.name,
                                       quantity=line.quantity, unit_price=line.item.selling_price, amount=amount)
            total += amount
        invoice.total_amount = total
        invoice.save(update_fields=["total_amount"])
        self._post("sales_invoice", invoice.id, self.users["sales"])
        return invoice

    def _customer_payment(self, invoice, amount, days_ago, reference):
        payment = CustomerPayment.objects.create(
            company=self.company, customer=invoice.customer, invoice=invoice, amount=amount,
            payment_date=self._day(days_ago), method="bank_transfer", reference=reference,
        )
        invoice.apply_payment(amount)
        self._post("customer_payment", payment.id, self.users["sales"])

    def _journal(self, days_ago, description, rows):
        entry = JournalEntry.objects.create(
            company=self.company, transaction_date=self._day(days_ago), reference=f"GJ-{self._day(days_ago):%m%d}",
            description=description, source_module="manual", created_by=self.users["finance"], status="draft",
        )
        for n, (code, debit, credit) in enumerate(rows, start=1):
            JournalEntryLine.objects.create(
                company=self.company, journal_entry=entry, line_number=n,
                account=Account.objects.get(company=self.company, code=code),
                debit=D(debit), credit=D(credit), description=description,
            )
        post_journal_entry(entry.id, self.users["finance"], company=self.company)

    def _post(self, event, source_id, user, payload=None):
        log = execute_auto_post(event, self.company, source_id, user=user, payload=payload)
        if log.status == "failed":
            raise RuntimeError(f"Auto-posting {event} #{source_id} failed: {log.message}")

    def _report(self, password):
        c = self.company
        out = self.stdout
        out.write(self.style.SUCCESS(f"=== {c.name} demo company seeded (slug {c.slug}) ==="))
        for role, user in self.users.items():
            out.write(f"  {role:<10} {user.username}")
        out.write(f"  password   {password}  (all users)")
        out.write(f"Items: {Item.objects.filter(company=c).count()}  Recipes/BOMs: {Recipe.objects.filter(product__company=c).count()}")
        out.write(f"Purchase orders: {PurchaseOrder.objects.filter(vendor__company=c).count()}  "
                  f"Sales orders: {SalesOrder.objects.filter(customer__company=c).count()}  "
                  f"Production orders: {ProductionOrder.objects.filter(recipe__product__company=c).count()}")
        out.write(f"Invoices: {Invoice.objects.filter(company=c).count()}  "
                  f"Customer payments: {CustomerPayment.objects.filter(company=c).count()}")
        out.write(f"Posted journal entries: {JournalEntry.objects.filter(company=c, status='posted').count()}")
        plan, requisition, dispatch = (getattr(self, name, None) for name in ("plan", "requisition", "dispatch"))
        out.write(f"Manufacturing: {Resource.objects.filter(company=c).count()} resources, "
                  f"{RoutingStep.objects.filter(recipe__product__company=c).count()} routing steps"
                  + (f"; plan {plan.plan_number}" if plan else "")
                  + (f", requisition {requisition.requisition_number}" if requisition else "")
                  + (f"; dispatch {dispatch.dispatch_number} invoiced" if dispatch else ""))
        out.write("QuickBooks: not connected. Accounting > QuickBooks > Connect, finish the setup wizard "
                  "(it opens at customer mapping), then 'Send everything to QuickBooks'.")
