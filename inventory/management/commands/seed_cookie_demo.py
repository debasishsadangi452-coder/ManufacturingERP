"""Seed a demo cookie bakery under any company name, showing a two-level BOM
end to end: procurement -> manufacturing -> sales -> accounting.

    python manage.py seed_cookie_demo --company "Dummy Cookies"
    python manage.py seed_cookie_demo --company "Dummy Cookies" --slug dummycookies
        --admin-username admin@dummycookies --password demo12345 [--brand Acme] [--replace]

Product structure
  BOM 1  Baked Cookie (intermediate, per 1,000 cookies):
         white flour, brown flour, sugar, coconut, dates, almonds, flavoring, colors
  BOM 2  Smarties Cookies 300g Bag (finished good, per 120 bags):
         1,440 baked cookies, Smarties candy, labels, bags, cartons

Raw materials are the only purchased items; the baked cookie is made and then
consumed in-house (held in 1225 Semi-Finished Goods); only the bag is sold.

History (all dates in the current month, posted to the ledger by auto-posting):
  * 5 purchase orders received (vendor bills auto-created; 1 paid, 1 part-paid),
    plus 2 open purchase orders
  * 3 baking runs (18,000 cookies) and 2 packing runs (1,200 bags), each QC
    approved; 3,600 baked cookies stay in stock. A scheduled packing run of 480
    bags needs 5,760 cookies, so it shows a "bake more cookies first" warning
  * Each recipe has a default line (baking / packing) used automatically
  * 4 sales orders shipped and invoiced (1 paid, 1 part-paid), plus 2 open orders
  * ERP-only journals: owner capital, rent, oven power, freight, salaries

Same options and safety as seed_welding_wire_demo: the command stops if the
slug exists, unless --replace (which DELETES that company first).
"""
from decimal import Decimal

from production.models import ProductionLine, ProductionOrder, Recipe, RecipeIngredient
from inventory.models import BOM, BOMLine, Warehouse
from procurement.models import Vendor, VendorPriceList
from sales.models import Customer

from .seed_welding_wire_demo import Command as DemoCommand

D = Decimal


class Command(DemoCommand):
    help = "Seed a cookie bakery demo company (two-level BOM) with a month of operating and accounting history."
    QC = ("Bake check", "Moisture, colour, diameter", "Within spec", "Moisture < 4%, 65 mm", "Released to packing")
    PACK_QC = ("Pack check", "Seal, label, net weight", "Within spec", "300 g +/- 5 g, sealed", "Released for sale")

    def build_history(self):
        company = self.company
        plant = Warehouse.objects.create(company=company, name="Bakery Plant", location="Production floor")
        fg = Warehouse.objects.create(company=company, name="Finished Goods Warehouse", location="Dispatch dock")
        baking = ProductionLine.objects.create(company=company, name="Mixing & Baking Line", capacity=10000)
        packing = ProductionLine.objects.create(company=company, name="Packing Line", capacity=800)

        rm = self._raw_materials()
        cookie, bag = self._products()
        bake_recipe, pack_recipe = self._recipes_and_boms(rm, cookie, bag)
        # Predefined lines: orders for each product (incl. ones planned from a
        # sales order) are assigned to these automatically.
        for recipe, line in ((bake_recipe, baking), (pack_recipe, packing)):
            recipe.default_line = line
            recipe.save(update_fields=["default_line"])
        vendors = self._vendors(rm)
        customers = self._customers()

        # --- Procurement: raw materials only ----------------------------------------
        po_flour = self._po(vendors["mill"], [(rm["white_flour"], 300, "0.90"), (rm["brown_flour"], 150, "1.10")],
                            "ordered", 28)
        po_pantry = self._po(vendors["pantry"], [
            (rm["sugar"], 200, "1.20"), (rm["coconut"], 50, "4.50"), (rm["dates"], 75, "6.00"), (rm["almonds"], 60, "9.50"),
        ], "ordered", 28)
        po_flavor = self._po(vendors["flavor"], [(rm["flavoring"], 5, "22.00"), (rm["colors"], 3, "18.00")], "ordered", 27)
        po_candy = self._po(vendors["candy"], [(rm["smarties"], 150, "8.00")], "ordered", 27)
        po_pack = self._po(vendors["pack"], [(rm["labels"], 10000, "0.04"), (rm["bags"], 10000, "0.08"),
                                             (rm["cartons"], 1000, "0.60")], "ordered", 26)
        bills = {}
        for key, po, days in [("mill", po_flour, 25), ("pantry", po_pantry, 25), ("flavor", po_flavor, 24),
                              ("candy", po_candy, 24), ("pack", po_pack, 23)]:
            bills[key] = self._receive(po, plant, days)
        self._pay_bill(bills["mill"], bills["mill"].total_amount, 10, "ACH-20417")
        self._pay_bill(bills["pantry"], D("1000.00"), 5, "CHQ-3381")
        # Open purchase orders (not yet received)
        self._po(vendors["mill"], [(rm["white_flour"], 300, "0.90")], "ordered", 3)
        self._po(vendors["candy"], [(rm["smarties"], 100, "8.00")], "draft", 1)

        # --- Manufacturing, level 1: bake cookies (intermediate, stays in the plant) --
        for days in (20, 18, 16):
            self._produce(bake_recipe, 6000, baking, plant, plant, days)
        # --- Manufacturing, level 2: pack cookies + Smarties into bags (finished good) -
        for days in (15, 12):
            self._produce(pack_recipe, 600, packing, plant, fg, days, qc=self.PACK_QC)
        # Scheduled packing run larger than the cookies on hand (needs 5,760, 3,600 in
        # stock): the Production screen warns to bake 2,160 more cookies first.
        ProductionOrder.objects.create(recipe=pack_recipe, quantity=480, warehouse=fg, line=packing,
                                       status="scheduled", start_time=self._at(-2))

        # --- Sales: finished bags only -------------------------------------------------
        so1 = self._sell(customers["freshmart"], [(bag, 400)], fg, 11)
        so2 = self._sell(customers["cafe"], [(bag, 200)], fg, 9)
        so3 = self._sell(customers["treats"], [(bag, 150)], fg, 7)
        so4 = self._sell(customers["campus"], [(bag, 250)], fg, 5)
        inv1 = self._invoice(so1, 11)
        inv2 = self._invoice(so2, 9)
        self._invoice(so3, 7)
        self._invoice(so4, 5)
        self._customer_payment(inv1, inv1.total_amount, 3, "WIRE-55120")
        self._customer_payment(inv2, D("300.00"), 2, "CHQ-90214")
        # Open sales orders
        self._order(customers["freshmart"], [(bag, 300)], "confirmed", 1)
        self._order(customers["deli"], [(bag, 100)], "pending", 1)

        # --- ERP-only accounting (sent to QuickBooks as journal entries) ---------------
        self._journal(30, "Owner capital contribution", [("1010", "60000.00", "0"), ("3010", "0", "60000.00")])
        self._journal(29, "Bakery rent", [("6050", "2500.00", "0"), ("1010", "0", "2500.00")])
        self._journal(10, "Oven gas & electricity", [("5200", "900.00", "0"), ("1010", "0", "900.00")])
        self._journal(6, "Delivery van fuel & freight", [("6030", "350.00", "0"), ("1010", "0", "350.00")])
        self._journal(1, "Admin & sales salaries", [("6010", "4200.00", "0"), ("1010", "0", "4200.00")])

    # ------------------------------------------------------------------ catalogue

    def _raw_materials(self):
        rm = lambda name, unit, uom, cost, sku: self._item(name, "raw_material", unit, uom, cost, sku=sku)  # noqa: E731
        return {
            "white_flour": rm("White Flour", "kg", "kg", "0.90", "RM-FLOUR-WHT"),
            "brown_flour": rm("Brown Flour", "kg", "kg", "1.10", "RM-FLOUR-BRN"),
            "sugar": rm("Sugar", "kg", "kg", "1.20", "RM-SUGAR"),
            "coconut": rm("Desiccated Coconut", "kg", "kg", "4.50", "RM-COCONUT"),
            "dates": rm("Dates (pitted)", "kg", "kg", "6.00", "RM-DATES"),
            "almonds": rm("Almonds (chopped)", "kg", "kg", "9.50", "RM-ALMONDS"),
            "flavoring": rm("Vanilla Flavoring", "kg", "kg", "22.00", "RM-FLAVOR-VAN"),
            "colors": rm("Artificial Food Colors", "kg", "kg", "18.00", "RM-COLORS"),
            "smarties": rm("Smarties Candy", "kg", "kg", "8.00", "RM-SMARTIES"),
            "labels": rm("Printed Label", "each", "each", "0.04", "PK-LABEL"),
            "bags": rm("Cookie Bag 300g", "each", "each", "0.08", "PK-BAG-300"),
            "cartons": rm("Shipping Carton (12 bags)", "each", "each", "0.60", "PK-CARTON-12"),
        }

    def _products(self):
        brand = f"{self.brand} " if self.brand else ""
        cookie = self._item("Baked Cookie", "intermediate", "each", "each", sku="INT-COOKIE")
        bag = self._item(f"{brand}Smarties Cookies 300g Bag", "finished_good", "bag", "each", price="3.20",
                         sku="FG-SMARTIES-300")
        return cookie, bag

    def _recipes_and_boms(self, rm, cookie, bag):
        # (product, batch size, [(input item, quantity per batch)])
        specs = [
            (cookie, 1000, [(rm["white_flour"], 12), (rm["brown_flour"], 6), (rm["sugar"], 8), (rm["coconut"], 2),
                            (rm["dates"], 3), (rm["almonds"], 2.5), (rm["flavoring"], 0.2), (rm["colors"], 0.1)]),
            (bag, 120, [(cookie, 1440), (rm["smarties"], 7.2), (rm["labels"], 120), (rm["bags"], 120),
                        (rm["cartons"], 10)]),
        ]
        recipes = []
        for product, batch_size, parts in specs:
            recipe = Recipe.objects.create(product=product, batch_size=batch_size)
            bom = BOM.objects.create(finished_good=product)
            for item, qty in parts:
                RecipeIngredient.objects.create(recipe=recipe, item=item, quantity=qty)
                BOMLine.objects.create(bom=bom, raw_material=item, quantity=round(qty / batch_size, 6),
                                       unit=item.unit, unit_of_measure=item.base_unit)
            recipes.append(recipe)
        return recipes

    def _vendors(self, rm):
        vendors = {
            "mill": Vendor.objects.create(company=self.company, name="Valley Flour Mills",
                                          email="orders@valleyflour.example", payment_terms="Net 30"),
            "pantry": Vendor.objects.create(company=self.company, name="Golden Harvest Foods",
                                            email="sales@goldenharvest.example", payment_terms="Net 30"),
            "flavor": Vendor.objects.create(company=self.company, name="Aroma Flavor House",
                                            email="ar@aromaflavor.example", payment_terms="Due on receipt"),
            "candy": Vendor.objects.create(company=self.company, name="Sweet Candy Distributors",
                                           email="orders@sweetcandy.example", payment_terms="Net 15"),
            "pack": Vendor.objects.create(company=self.company, name="PackRight Packaging",
                                          email="orders@packright.example", payment_terms="Net 30"),
        }
        supply = {"mill": ("white_flour", "brown_flour"), "pantry": ("sugar", "coconut", "dates", "almonds"),
                  "flavor": ("flavoring", "colors"), "candy": ("smarties",), "pack": ("labels", "bags", "cartons")}
        for v_key, rm_keys in supply.items():
            for rm_key in rm_keys:
                VendorPriceList.objects.create(vendor=vendors[v_key], item=rm[rm_key],
                                               unit_price=rm[rm_key].purchase_cost, min_order_qty=1, lead_time_days=5)
        return vendors

    def _customers(self):
        spec = {
            "freshmart": ("FreshMart Supermarkets", "buying@freshmart.example", "Net 30"),
            "cafe": ("Corner Cafe Chain", "accounts@cornercafe.example", "Net 15"),
            "treats": ("Little Treats Kids Store", "orders@littletreats.example", "Due on receipt"),
            "campus": ("Campus Snack Co", "procurement@campussnack.example", "Net 30"),
            "deli": ("Harbor Deli", "owner@harbordeli.example", "Due on receipt"),
        }
        return {k: Customer.objects.create(company=self.company, name=n, email=e, payment_terms=t)
                for k, (n, e, t) in spec.items()}
