"""Seed the Red Velvet NYC bakery catalog: finished goods, raw materials,
recipes, BOMs, vendors, price lists, stock and the two production lines.

Idempotent — re-running updates in place rather than duplicating. Everything is
scoped to the `redvelvetnyc` company (owner.admin@redvelvetnyc).

Run:  python seed_redvelvet_data.py
"""

import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "freshfizz_erp.settings")
django.setup()

from decimal import Decimal

from django.utils import timezone

from accounts.models import Company
from inventory.models import (
    BOM, BOMLine, Item, Stock, UnitOfMeasure, Warehouse,
)
from procurement.models import Vendor, VendorPriceList
from production.models import ProductionLine, Recipe, RecipeIngredient

COMPANY_SLUG = "redvelvetnyc"

# Mass conversions used when stating recipe quantities.
G_PER_OZ = 28.34952312
G_PER_LB = 453.59237


# ---------------------------------------------------------------------------
# Raw materials — name, purchase cost per lb (or per each for count items),
# reorder point in grams, and whether it is a mass or count material.
# ---------------------------------------------------------------------------
RAW_MATERIALS = [
    # (name, sku_suffix, dimension, purchase_cost, reorder_point)
    ("All-Purpose Flour",          "AP-FLOUR",        "mass",  0.60,  22700),
    ("Heat-Treated Flour",         "HT-FLOUR",        "mass",  1.10,   9080),
    ("Almond Flour",               "ALMOND-FLOUR",    "mass",  6.50,   4540),
    ("Gluten-Free Flour Blend",    "GF-FLOUR",        "mass",  3.20,   4540),
    ("Cane Sugar",                 "CANE-SUGAR",      "mass",  1.20,  18160),
    ("Powdered Sugar",             "POWDERED-SUGAR",  "mass",  1.45,   6810),
    ("Brown Sugar",                "BROWN-SUGAR",     "mass",  1.35,   9080),
    ("Vanilla Sugar",              "VANILLA-SUGAR",   "mass",  4.80,   2270),
    ("Dutch Cocoa Powder",         "COCOA-POWDER",    "mass",  5.20,   6810),
    ("Semisweet Chocolate Chips",  "CHOC-CHIPS",      "mass",  3.50,  13620),
    ("White Chocolate Chips",      "WHITE-CHOC",      "mass",  4.10,   4540),
    ("Dark Couverture Chocolate",  "DARK-COUVERTURE", "mass",  8.90,   4540),
    ("Unsalted Butter",            "BUTTER",          "mass",  3.75,  18160),
    ("Cream Cheese",               "CREAM-CHEESE",    "mass",  3.20,   9080),
    ("Heavy Cream",                "HEAVY-CREAM",     "mass",  2.85,   9080),
    ("Whole Milk",                 "WHOLE-MILK",      "mass",  0.95,   9080),
    ("Eggs (Grade A)",             "EGGS",            "count", 0.28,   2400),
    ("Egg Whites (Pasteurized)",   "EGG-WHITES",      "mass",  4.40,   4540),
    ("Almond Extract",             "ALMOND-EXTRACT",  "mass", 22.00,    900),
    ("Vanilla Extract",            "VANILLA-EXTRACT", "mass", 26.00,   1360),
    ("Baking Soda",                "BAKING-SODA",     "mass",  0.85,   2270),
    ("Baking Powder",              "BAKING-POWDER",   "mass",  1.90,   2270),
    ("Cream of Tartar",            "CREAM-TARTAR",    "mass",  7.40,    900),
    ("Fine Sea Salt",              "SEA-SALT",        "mass",  0.70,   2270),
    ("Vietnamese Cinnamon",        "CINNAMON",        "mass", 14.50,   1360),
    ("Ground Ginger",              "GINGER",          "mass", 11.00,    900),
    ("Molasses",                   "MOLASSES",        "mass",  2.40,   2270),
    ("Rainbow Sprinkles",          "SPRINKLES",       "mass",  4.60,   2270),
    ("Nutella Hazelnut Spread",    "NUTELLA",         "mass",  5.80,   4540),
    ("Roasted Hazelnuts",          "HAZELNUTS",       "mass", 10.20,   2270),
    ("Sliced Almonds",             "SLICED-ALMONDS",  "mass",  9.40,   2270),
    ("Raspberry Preserves",        "RASPBERRY-JAM",   "mass",  4.90,   2270),
    ("Strawberry Puree",           "STRAWBERRY",      "mass",  3.60,   2270),
    ("Salted Caramel Sauce",       "SALTED-CARAMEL",  "mass",  6.30,   2270),
    ("Marshmallow Fluff",          "MARSHMALLOW",     "mass",  3.90,   2270),
    ("Graham Cracker Crumbs",      "GRAHAM-CRUMBS",   "mass",  3.10,   2270),
    ("Red Food Coloring",          "RED-COLOR",       "mass", 18.00,    450),
    ("Pink Food Coloring",         "PINK-COLOR",      "mass", 18.00,    450),
    ("Granny Smith Apples",        "APPLES",          "mass",  1.80,   9080),
    ("Pineapple Rings",            "PINEAPPLE",       "mass",  2.60,   4540),
    ("Black Peppercorns",          "BLACK-PEPPER",    "mass", 13.00,    450),
    ("Egg Yolks (Pasteurized)",    "EGG-YOLKS",       "mass",  5.60,   2270),
    # Packaging
    ("Cast Iron Skillet Pan (8in)", "PAN-SKILLET",    "count", 3.40,    600),
    ("5oz Dough Cup with Lid",     "PKG-CUP-5OZ",     "count", 0.22,   4000),
    ("Wooden Spoon (mini)",        "PKG-SPOON",       "count", 0.04,   4000),
    ("Bakery Box (12ct)",          "PKG-BOX-12",      "count", 0.55,   2000),
    ("Cupcake Liners",             "PKG-LINERS",      "count", 0.03,   6000),
    ("Shrink Wrap Film",           "PKG-FILM",        "mass",  1.10,   2270),
]


# ---------------------------------------------------------------------------
# Finished goods, straight from the catalog. `line` selects which production
# line makes it: "dough" (ready-to-bake / ready-to-eat doughs) or "cookies"
# (baked cookies, cupcakes, cakes and desserts).
# ---------------------------------------------------------------------------
FINISHED_GOODS = [
    # (name, price, unit, line, batch_size, level, gluten_free, sold_out, note)
    # --- Dough line ---
    ("Ready-to-Bake Cookie Dough Skillet", 24.00, "each", "dough", 60, "Easy", False, False,
     "Shareable ready-to-bake skillet with gooey meter on pack"),
    ("Ready-to-Eat Cookie Dough Cup (5oz)", 12.00, "each", "dough", 120, "Easy", False, False,
     "Egg-free, heat-treated flour, safe to eat by the spoonful"),
    # --- Cookies ---
    ("Amaretti Cookies", 28.00, "each", "cookies", 48, "Easy", True, False,
     "with Almond & Cocoa"),
    ("Brownies", 30.00, "each", "cookies", 36, "Moderate", False, False,
     "with salted caramel"),
    ("Chocolate Crinkle Cookies", 28.00, "each", "cookies", 48, "Easy", False, False,
     "with Cocoa & Sugar"),
    ("Chocolate Truffles", 32.00, "each", "cookies", 60, "Easy", True, False,
     "with Cocoa & Sugar"),
    ("French Macarons", 34.00, "each", "cookies", 40, "Advanced", True, False,
     "with Raspberry Preserves"),
    ("Ginger Snap Cookies", 30.00, "each", "cookies", 48, "Easy", False, True,
     "with white chocolate - SOLD OUT"),
    ("Neapolitan Cookies", 30.00, "each", "cookies", 48, "Easy", False, False,
     "with Chocolate & Strawberry"),
    ("Snickerdoodle Cookies", 28.00, "each", "cookies", 48, "Easy", False, False,
     "with Vietnamese Cinnamon"),
    # --- Cupcakes (18 per box) ---
    ("Celebration Cupcakes (18)", 32.00, "each", "cookies", 36, "Easy", False, False,
     "with Colorful Sprinkles"),
    ("Nutella Cupcakes (18)", 32.00, "each", "cookies", 36, "Easy", False, False,
     "with Roasted Hazelnuts"),
    ("Pink Velvet Cupcakes (18)", 33.00, "each", "cookies", 36, "Easy", False, False,
     "with Cream Cheese Frosting"),
    ("Red Velvet Cupcakes (18)", 32.00, "each", "cookies", 36, "Easy", False, False,
     "with Cream Cheese Frosting"),
    ("S'mores Cupcakes (18)", 32.00, "each", "cookies", 36, "Moderate", False, False,
     "with Marshmallow Frosting"),
    # --- Cakes & desserts ---
    ("Apple Spice Cake", 30.00, "each", "cookies", 24, "Moderate", False, False,
     "Toasted almonds top this moist apple and spice sponge cake"),
    ("Fancy Birthday Cake", 36.00, "each", "cookies", 20, "Moderate", False, False,
     "with colorful sprinkles"),
    ("Creme Brulee", 28.00, "each", "cookies", 30, "Advanced", True, False,
     "with Vanilla Sugar"),
    ("Devil's Food Cake", 30.00, "each", "cookies", 24, "Easy", False, False,
     "with Cocoa Glaze"),
    ("Molten Chocolate Cake", 28.00, "each", "cookies", 30, "Advanced", False, False,
     "with Lava Center"),
    ("Pineapple Upside Down Cake", 30.00, "each", "cookies", 24, "Moderate", False, False,
     "with Black Pepper Caramel"),
]


# ---------------------------------------------------------------------------
# Recipes: quantities are per BATCH, in ounces for mass materials and in each
# for count materials (matching how a bakery scales a mixer bowl).
# ---------------------------------------------------------------------------
RECIPES = {
    "Ready-to-Bake Cookie Dough Skillet": [
        ("All-Purpose Flour", 320, "oz"), ("Unsalted Butter", 200, "oz"),
        ("Brown Sugar", 160, "oz"), ("Cane Sugar", 120, "oz"),
        ("Semisweet Chocolate Chips", 200, "oz"), ("Eggs (Grade A)", 24, "each"),
        ("Vanilla Extract", 6, "oz"), ("Baking Soda", 3, "oz"),
        ("Fine Sea Salt", 2.5, "oz"), ("Cast Iron Skillet Pan (8in)", 60, "each"),
        ("Shrink Wrap Film", 10, "oz"),
    ],
    "Ready-to-Eat Cookie Dough Cup (5oz)": [
        ("Heat-Treated Flour", 300, "oz"), ("Unsalted Butter", 190, "oz"),
        ("Brown Sugar", 150, "oz"), ("Cane Sugar", 110, "oz"),
        ("Semisweet Chocolate Chips", 180, "oz"), ("Vanilla Extract", 5, "oz"),
        ("Fine Sea Salt", 2, "oz"), ("5oz Dough Cup with Lid", 120, "each"),
        ("Wooden Spoon (mini)", 120, "each"),
    ],
    "Amaretti Cookies": [
        ("Almond Flour", 96, "oz"), ("Powdered Sugar", 80, "oz"),
        ("Egg Whites (Pasteurized)", 40, "oz"), ("Almond Extract", 2, "oz"),
        ("Dutch Cocoa Powder", 12, "oz"), ("Bakery Box (12ct)", 4, "each"),
    ],
    "Brownies": [
        ("All-Purpose Flour", 72, "oz"), ("Dutch Cocoa Powder", 40, "oz"),
        ("Unsalted Butter", 96, "oz"), ("Cane Sugar", 128, "oz"),
        ("Eggs (Grade A)", 18, "each"), ("Dark Couverture Chocolate", 48, "oz"),
        ("Salted Caramel Sauce", 32, "oz"), ("Fine Sea Salt", 1.5, "oz"),
        ("Bakery Box (12ct)", 3, "each"),
    ],
    "Chocolate Crinkle Cookies": [
        ("All-Purpose Flour", 112, "oz"), ("Dutch Cocoa Powder", 36, "oz"),
        ("Cane Sugar", 96, "oz"), ("Powdered Sugar", 32, "oz"),
        ("Eggs (Grade A)", 16, "each"), ("Unsalted Butter", 64, "oz"),
        ("Baking Powder", 2, "oz"), ("Bakery Box (12ct)", 4, "each"),
    ],
    "Chocolate Truffles": [
        ("Dark Couverture Chocolate", 160, "oz"), ("Heavy Cream", 80, "oz"),
        ("Unsalted Butter", 24, "oz"), ("Dutch Cocoa Powder", 24, "oz"),
        ("Cane Sugar", 16, "oz"), ("Bakery Box (12ct)", 5, "each"),
    ],
    "French Macarons": [
        ("Almond Flour", 72, "oz"), ("Powdered Sugar", 88, "oz"),
        ("Egg Whites (Pasteurized)", 44, "oz"), ("Cane Sugar", 32, "oz"),
        ("Raspberry Preserves", 40, "oz"), ("Cream of Tartar", 0.5, "oz"),
        ("Bakery Box (12ct)", 4, "each"),
    ],
    "Ginger Snap Cookies": [
        ("All-Purpose Flour", 120, "oz"), ("Brown Sugar", 88, "oz"),
        ("Molasses", 40, "oz"), ("Ground Ginger", 6, "oz"),
        ("Vietnamese Cinnamon", 4, "oz"), ("Unsalted Butter", 72, "oz"),
        ("Eggs (Grade A)", 12, "each"), ("White Chocolate Chips", 56, "oz"),
        ("Baking Soda", 2, "oz"), ("Bakery Box (12ct)", 4, "each"),
    ],
    "Neapolitan Cookies": [
        ("All-Purpose Flour", 128, "oz"), ("Cane Sugar", 96, "oz"),
        ("Unsalted Butter", 80, "oz"), ("Eggs (Grade A)", 14, "each"),
        ("Semisweet Chocolate Chips", 48, "oz"), ("Strawberry Puree", 32, "oz"),
        ("Vanilla Extract", 3, "oz"), ("Pink Food Coloring", 0.5, "oz"),
        ("Bakery Box (12ct)", 4, "each"),
    ],
    "Snickerdoodle Cookies": [
        ("All-Purpose Flour", 128, "oz"), ("Cane Sugar", 104, "oz"),
        ("Unsalted Butter", 80, "oz"), ("Eggs (Grade A)", 14, "each"),
        ("Vietnamese Cinnamon", 8, "oz"), ("Cream of Tartar", 2, "oz"),
        ("Baking Soda", 1.5, "oz"), ("Bakery Box (12ct)", 4, "each"),
    ],
    "Celebration Cupcakes (18)": [
        ("All-Purpose Flour", 180, "oz"), ("Cane Sugar", 144, "oz"),
        ("Unsalted Butter", 120, "oz"), ("Eggs (Grade A)", 36, "each"),
        ("Whole Milk", 72, "oz"), ("Rainbow Sprinkles", 32, "oz"),
        ("Vanilla Extract", 5, "oz"), ("Baking Powder", 4, "oz"),
        ("Cupcake Liners", 648, "each"), ("Bakery Box (12ct)", 36, "each"),
    ],
    "Nutella Cupcakes (18)": [
        ("All-Purpose Flour", 180, "oz"), ("Cane Sugar", 132, "oz"),
        ("Unsalted Butter", 120, "oz"), ("Eggs (Grade A)", 36, "each"),
        ("Nutella Hazelnut Spread", 96, "oz"), ("Roasted Hazelnuts", 40, "oz"),
        ("Whole Milk", 64, "oz"), ("Baking Powder", 4, "oz"),
        ("Cupcake Liners", 648, "each"), ("Bakery Box (12ct)", 36, "each"),
    ],
    "Pink Velvet Cupcakes (18)": [
        ("All-Purpose Flour", 180, "oz"), ("Cane Sugar", 140, "oz"),
        ("Unsalted Butter", 116, "oz"), ("Eggs (Grade A)", 36, "each"),
        ("Cream Cheese", 96, "oz"), ("Powdered Sugar", 72, "oz"),
        ("Pink Food Coloring", 3, "oz"), ("Whole Milk", 64, "oz"),
        ("Baking Powder", 4, "oz"), ("Cupcake Liners", 648, "each"),
        ("Bakery Box (12ct)", 36, "each"),
    ],
    "Red Velvet Cupcakes (18)": [
        ("All-Purpose Flour", 180, "oz"), ("Cane Sugar", 140, "oz"),
        ("Unsalted Butter", 116, "oz"), ("Eggs (Grade A)", 36, "each"),
        ("Cream Cheese", 96, "oz"), ("Powdered Sugar", 72, "oz"),
        ("Dutch Cocoa Powder", 12, "oz"), ("Red Food Coloring", 4, "oz"),
        ("Whole Milk", 64, "oz"), ("Baking Powder", 4, "oz"),
        ("Cupcake Liners", 648, "each"), ("Bakery Box (12ct)", 36, "each"),
    ],
    "S'mores Cupcakes (18)": [
        ("All-Purpose Flour", 176, "oz"), ("Graham Cracker Crumbs", 64, "oz"),
        ("Cane Sugar", 132, "oz"), ("Unsalted Butter", 120, "oz"),
        ("Eggs (Grade A)", 36, "each"), ("Marshmallow Fluff", 88, "oz"),
        ("Dark Couverture Chocolate", 56, "oz"), ("Whole Milk", 64, "oz"),
        ("Baking Powder", 4, "oz"), ("Cupcake Liners", 648, "each"),
        ("Bakery Box (12ct)", 36, "each"),
    ],
    "Apple Spice Cake": [
        ("All-Purpose Flour", 160, "oz"), ("Granny Smith Apples", 192, "oz"),
        ("Brown Sugar", 112, "oz"), ("Unsalted Butter", 96, "oz"),
        ("Eggs (Grade A)", 24, "each"), ("Sliced Almonds", 48, "oz"),
        ("Vietnamese Cinnamon", 5, "oz"), ("Baking Powder", 3, "oz"),
        ("Bakery Box (12ct)", 24, "each"),
    ],
    "Fancy Birthday Cake": [
        ("All-Purpose Flour", 160, "oz"), ("Cane Sugar", 144, "oz"),
        ("Unsalted Butter", 128, "oz"), ("Eggs (Grade A)", 30, "each"),
        ("Heavy Cream", 72, "oz"), ("Powdered Sugar", 96, "oz"),
        ("Rainbow Sprinkles", 40, "oz"), ("Vanilla Extract", 6, "oz"),
        ("Baking Powder", 3, "oz"), ("Bakery Box (12ct)", 20, "each"),
    ],
    "Creme Brulee": [
        ("Heavy Cream", 240, "oz"), ("Egg Yolks (Pasteurized)", 72, "oz"),
        ("Vanilla Sugar", 64, "oz"), ("Cane Sugar", 40, "oz"),
        ("Vanilla Extract", 3, "oz"),
    ],
    "Devil's Food Cake": [
        ("All-Purpose Flour", 144, "oz"), ("Dutch Cocoa Powder", 56, "oz"),
        ("Cane Sugar", 144, "oz"), ("Unsalted Butter", 104, "oz"),
        ("Eggs (Grade A)", 24, "each"), ("Whole Milk", 80, "oz"),
        ("Dark Couverture Chocolate", 48, "oz"), ("Baking Soda", 3, "oz"),
        ("Bakery Box (12ct)", 24, "each"),
    ],
    "Molten Chocolate Cake": [
        ("Dark Couverture Chocolate", 160, "oz"), ("Unsalted Butter", 120, "oz"),
        ("Eggs (Grade A)", 30, "each"), ("Cane Sugar", 80, "oz"),
        ("All-Purpose Flour", 40, "oz"), ("Dutch Cocoa Powder", 16, "oz"),
        ("Bakery Box (12ct)", 30, "each"),
    ],
    "Pineapple Upside Down Cake": [
        ("All-Purpose Flour", 152, "oz"), ("Pineapple Rings", 168, "oz"),
        ("Brown Sugar", 128, "oz"), ("Unsalted Butter", 112, "oz"),
        ("Eggs (Grade A)", 24, "each"), ("Salted Caramel Sauce", 48, "oz"),
        ("Black Peppercorns", 1, "oz"), ("Baking Powder", 3, "oz"),
        ("Bakery Box (12ct)", 24, "each"),
    ],
}


VENDORS = [
    ("Sweet Supply Co", "raw_material", "orders@sweetsupply.com", "+1-212-555-0142",
     "410 Food Center Dr, Bronx, NY 10474", 4.8, "Net 30",
     ["Cane Sugar", "Powdered Sugar", "Brown Sugar", "Vanilla Sugar", "Molasses"]),
    ("Hudson Valley Mills", "raw_material", "sales@hvmills.com", "+1-845-555-0198",
     "27 Mill Rd, Poughkeepsie, NY 12601", 4.6, "Net 30",
     ["All-Purpose Flour", "Heat-Treated Flour", "Almond Flour",
      "Gluten-Free Flour Blend", "Graham Cracker Crumbs"]),
    ("Cacao Barry Imports", "raw_material", "us-orders@cacaobarry.com", "+1-212-555-0177",
     "88 Wall St, New York, NY 10005", 4.9, "Net 15",
     ["Dutch Cocoa Powder", "Semisweet Chocolate Chips", "White Chocolate Chips",
      "Dark Couverture Chocolate", "Nutella Hazelnut Spread"]),
    ("Catskill Creamery", "raw_material", "wholesale@catskillcreamery.com", "+1-518-555-0123",
     "1200 Dairy Ln, Hudson, NY 12534", 4.7, "Net 15",
     ["Unsalted Butter", "Cream Cheese", "Heavy Cream", "Whole Milk",
      "Eggs (Grade A)", "Egg Whites (Pasteurized)", "Egg Yolks (Pasteurized)"]),
    ("Spice Route Trading", "raw_material", "hello@spiceroute.com", "+1-718-555-0166",
     "55 Bushwick Ave, Brooklyn, NY 11206", 4.4, "Net 30",
     ["Vietnamese Cinnamon", "Ground Ginger", "Black Peppercorns", "Fine Sea Salt",
      "Vanilla Extract", "Almond Extract", "Baking Soda", "Baking Powder",
      "Cream of Tartar"]),
    ("Empire Produce Partners", "raw_material", "orders@empireproduce.com", "+1-212-555-0111",
     "355 Hunts Point Ave, Bronx, NY 10474", 4.3, "Net 15",
     ["Granny Smith Apples", "Pineapple Rings", "Strawberry Puree",
      "Raspberry Preserves", "Roasted Hazelnuts", "Sliced Almonds"]),
    ("Confection Extras LLC", "raw_material", "support@confectionextras.com", "+1-201-555-0154",
     "9 Industrial Way, Secaucus, NJ 07094", 4.2, "Net 30",
     ["Rainbow Sprinkles", "Red Food Coloring", "Pink Food Coloring",
      "Salted Caramel Sauce", "Marshmallow Fluff"]),
    ("MetroPack Solutions", "packaging", "sales@metropack.com", "+1-718-555-0190",
     "2100 Packaging Blvd, Queens, NY 11101", 4.5, "Net 45",
     ["Cast Iron Skillet Pan (8in)", "5oz Dough Cup with Lid", "Wooden Spoon (mini)",
      "Bakery Box (12ct)", "Cupcake Liners", "Shrink Wrap Film"]),
]


def uom(code):
    return UnitOfMeasure.objects.filter(code=code, company__isnull=True).first()


def to_grams(qty, unit_code):
    if unit_code == "oz":
        return qty * G_PER_OZ
    if unit_code == "lb":
        return qty * G_PER_LB
    return qty  # already grams or a count


def seed():
    company = Company.objects.get(slug=COMPANY_SLUG)
    print(f"Seeding catalog for {company.name} (#{company.id})\n")

    g, each, lb = uom("g"), uom("each"), uom("lb")
    now = timezone.now()

    # ---- Warehouses ------------------------------------------------------
    warehouses = {}
    for name, location in [
        ("Mount Kisco Plant", "Mount Kisco, NY"),
        ("Milton Staging", "Milton, NY"),
        ("Upstate Cold Storage", "Kingston, NY"),
    ]:
        wh, _ = Warehouse.objects.get_or_create(
            company=company, name=name, defaults={"location": location}
        )
        warehouses[name] = wh
    plant = warehouses["Mount Kisco Plant"]
    cold = warehouses["Upstate Cold Storage"]
    print(f"Warehouses: {len(warehouses)}")

    # ---- Production lines -------------------------------------------------
    dough_line, _ = ProductionLine.objects.update_or_create(
        company=company, name="Dough Line 1",
        defaults={
            "location": "Mount Kisco Plant - Bay A",
            "capacity": 480.0,          # dough units per hour
            "is_active": True,
            "status": "running",
        },
    )
    cookie_line, _ = ProductionLine.objects.update_or_create(
        company=company, name="Cookie & Bake Line 1",
        defaults={
            "location": "Mount Kisco Plant - Bay B",
            "capacity": 320.0,          # baked units per hour
            "is_active": True,
            "status": "running",
        },
    )
    lines = {"dough": dough_line, "cookies": cookie_line}
    print(f"Production lines: {dough_line.name}, {cookie_line.name}")

    # ---- Raw materials ----------------------------------------------------
    raw_items = {}
    for name, sku, dimension, cost, reorder in RAW_MATERIALS:
        base = g if dimension == "mass" else each
        purchase = lb if dimension == "mass" else each
        item, _ = Item.objects.update_or_create(
            company=company, name=name,
            defaults={
                "category": "raw_material",
                "unit": "lb" if dimension == "mass" else "each",
                "base_unit": base,
                "purchase_unit": purchase,
                "sku": f"RM-{sku}",
                "purchase_cost": Decimal(str(cost)),
                "reorder_point": reorder,
                "selling_price": Decimal("0.00"),
                "erp_classification": "raw_material",
                "classification_completed_at": now,
            },
        )
        raw_items[name] = item
    print(f"Raw materials: {len(raw_items)}")

    # ---- Finished goods ---------------------------------------------------
    fg_items = {}
    for (name, price, unit, line_key, batch_size, level,
         gluten_free, sold_out, note) in FINISHED_GOODS:
        slug = name.upper().replace("'", "").replace("(", "").replace(")", "")
        slug = "-".join(slug.split())[:60]
        item, _ = Item.objects.update_or_create(
            company=company, name=name,
            defaults={
                "category": "finished_good",
                "unit": unit,
                "base_unit": each,
                "purchase_unit": each,
                "sku": f"FG-{slug}",
                "selling_price": Decimal(str(price)),
                "purchase_cost": Decimal("0.00"),
                "erp_classification": "finished_good",
                "classification_completed_at": now,
                "bom_completed": True,
            },
        )
        fg_items[name] = item
    print(f"Finished goods: {len(fg_items)}")

    # ---- Recipes + BOMs ---------------------------------------------------
    fg_meta = {f[0]: f for f in FINISHED_GOODS}
    recipe_count = bom_line_count = 0

    for product_name, ingredients in RECIPES.items():
        product = fg_items[product_name]
        batch_size = fg_meta[product_name][4]

        recipe, _ = Recipe.objects.update_or_create(
            product=product, defaults={"batch_size": batch_size}
        )
        # Rebuild ingredients so re-runs reflect edits to RECIPES.
        recipe.recipeingredient_set.all().delete()

        bom, _ = BOM.objects.get_or_create(finished_good=product)
        bom.lines.all().delete()

        for raw_name, qty, unit_code in ingredients:
            raw = raw_items[raw_name]
            # RecipeIngredient.quantity is per batch, in the material's base unit.
            RecipeIngredient.objects.create(
                recipe=recipe, item=raw, quantity=to_grams(qty, unit_code)
            )
            # BOM lines are per finished unit, stated in the purchase-facing unit.
            BOMLine.objects.create(
                bom=bom,
                raw_material=raw,
                quantity=round(qty / batch_size, 4),
                unit=unit_code,
                unit_of_measure=uom(unit_code),
            )
            bom_line_count += 1
        recipe_count += 1
    print(f"Recipes: {recipe_count} (with {bom_line_count} BOM lines)")

    # ---- Vendors + price lists -------------------------------------------
    price_count = 0
    for (vname, category, email, phone, address, rating, terms, supplies) in VENDORS:
        vendor, _ = Vendor.objects.update_or_create(
            company=company, name=vname,
            defaults={
                "category": category, "email": email, "phone": phone,
                "address": address, "rating": rating, "payment_terms": terms,
            },
        )
        for raw_name in supplies:
            raw = raw_items[raw_name]
            VendorPriceList.objects.update_or_create(
                vendor=vendor, item=raw,
                defaults={
                    "unit_price": raw.purchase_cost,
                    "min_order_qty": 50,
                    "lead_time_days": 5 if category == "raw_material" else 10,
                    "is_active": True,
                },
            )
            price_count += 1
    print(f"Vendors: {len(VENDORS)} (with {price_count} price-list entries)")

    # ---- Opening stock ----------------------------------------------------
    # Raw materials land at ~3x their reorder point so every recipe can run.
    stock_rows = 0
    for name, item in raw_items.items():
        qty = (item.reorder_point or 1000) * 3
        Stock.objects.update_or_create(
            item=item, warehouse=plant, defaults={"quantity": qty}
        )
        stock_rows += 1

    # Finished goods: modest on-hand at the plant, a little in cold storage.
    # Ginger Snap Cookies is flagged sold out in the catalog, so it holds zero.
    for name, item in fg_items.items():
        sold_out = fg_meta[name][7]
        Stock.objects.update_or_create(
            item=item, warehouse=plant,
            defaults={"quantity": 0 if sold_out else 240},
        )
        Stock.objects.update_or_create(
            item=item, warehouse=cold,
            defaults={"quantity": 0 if sold_out else 120},
        )
        stock_rows += 2
    print(f"Stock rows: {stock_rows}")

    print("\nDone.")
    print(f"  Login: owner.admin@{COMPANY_SLUG}")
    print(f"  {len(fg_items)} finished goods, {len(raw_items)} raw materials, "
          f"{recipe_count} recipes, 2 production lines")


if __name__ == "__main__":
    seed()
