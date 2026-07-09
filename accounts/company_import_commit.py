"""Write the validated company-import data. Called only after validation
passes; runs in one transaction so a failure rolls everything back."""
from django.db import transaction


@transaction.atomic
def commit(company, user, item_defs, stock_rows, vendor_defs, price_rows,
          customer_rows, line_rows, recipe_rows):
    from inventory.models import Item, Warehouse, Stock
    from inventory.services import increase_stock
    from procurement.models import Vendor, VendorPriceList
    from sales.models import Customer
    from production.models import ProductionLine, Recipe, RecipeIngredient

    summary = {"items": 0, "warehouses": 0, "stock_entries": 0, "vendors": 0,
               "vendor_prices": 0, "customers": 0, "production_lines": 0, "recipes": 0}

    # Items (once per code)
    items = {}
    for code, d in item_defs.items():
        item, _ = Item.objects.get_or_create(
            company=company, name=d["name"],
            defaults={"category": d["category"], "unit": d["unit"],
                      "selling_price": d["selling_price"]},
        )
        items[code] = item
        summary["items"] += 1

    # Warehouses (auto-create) + opening stock per row
    warehouses = {}
    def wh(name):
        key = name.lower()
        if key not in warehouses:
            w, created = Warehouse.objects.get_or_create(company=company, name=name,
                                                         defaults={"location": name})
            warehouses[key] = w
            if created:
                summary["warehouses"] += 1
        return warehouses[key]

    for code, wh_name, qty in stock_rows:
        warehouse = wh(wh_name)
        if float(qty) > 0:
            increase_stock(items[code], warehouse, float(qty), user=user,
                           reference="Company import - opening stock")
        else:
            Stock.objects.get_or_create(item=items[code], warehouse=warehouse,
                                        defaults={"quantity": 0})
        summary["stock_entries"] += 1

    # Vendors + prices
    vendors = {}
    for name, d in vendor_defs.items():
        v, _ = Vendor.objects.get_or_create(
            company=company, name=name,
            defaults={"category": d["category"], "email": d["email"],
                      "phone": d["phone"], "address": d["address"], "rating": d["rating"]},
        )
        vendors[name] = v
        summary["vendors"] += 1
    for vname, code, price, currency, moq, lead in price_rows:
        VendorPriceList.objects.update_or_create(
            vendor=vendors[vname], item=items[code],
            defaults={"unit_price": price, "currency": currency,
                      "min_order_qty": moq, "lead_time_days": lead, "is_active": True},
        )
        summary["vendor_prices"] += 1

    # Customers
    for c in customer_rows:
        Customer.objects.get_or_create(
            company=company, name=c["name"],
            defaults={"email": c["email"], "phone": c["phone"], "address": c["address"]},
        )
        summary["customers"] += 1

    # Production lines
    for l in line_rows:
        ProductionLine.objects.get_or_create(
            company=company, name=l["name"],
            defaults={"location": l["location"], "capacity": l["capacity"]},
        )
        summary["production_lines"] += 1

    # Recipes (group ingredients by product)
    from collections import defaultdict
    by_product = defaultdict(list)
    for pcode, icode, qty in recipe_rows:
        by_product[pcode].append((icode, qty))
    for pcode, ingredients in by_product.items():
        recipe, _ = Recipe.objects.get_or_create(product=items[pcode])
        for icode, qty in ingredients:
            RecipeIngredient.objects.get_or_create(
                recipe=recipe, item=items[icode], defaults={"quantity": float(qty)})
        summary["recipes"] += 1

    return summary
