import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from inventory.models import Warehouse, Item, Stock
from procurement.models import Vendor, VendorPriceList
from production.models import ProductionLine
from decimal import Decimal

def seed_fizz_data():
    print("🥤 Seeding FreshFizz Data...")

    # 1. Warehouses
    wh_main, _ = Warehouse.objects.get_or_create(name="Fizz Central Warehouse", location="Chicago, IL")
    wh_raw, _ = Warehouse.objects.get_or_create(name="Fizz Raw Materials Hub", location="Gary, IN")
    wh_dist, _ = Warehouse.objects.get_or_create(name="Fizz Distribution Center", location="Naperville, IL")

    # 2. Items
    items_data = [
        {"name": "Fizz Classic Syrup (20L)", "category": "raw_material", "unit": "drum"},
        {"name": "Fizz Diet Concentrate (10L)", "category": "raw_material", "unit": "drum"},
        {"name": "Carbon Dioxide (CO2)", "category": "raw_material", "unit": "kg"},
        {"name": "Glass Bottles (500ml)", "category": "raw_material", "unit": "crate"},
        {"name": "Aluminum Cans (330ml)", "category": "raw_material", "unit": "pallet"},
        {"name": "Fizz Classic 500ml", "category": "finished_good", "unit": "bottle"},
        {"name": "Fizz Diet 330ml", "category": "finished_good", "unit": "can"},
    ]

    for item_info in items_data:
        item, created = Item.objects.get_or_create(
            name=item_info["name"],
            defaults={"category": item_info["category"], "unit": item_info["unit"]}
        )
        if created:
            # Initial stock
            Stock.objects.get_or_create(item=item, warehouse=wh_raw, defaults={"quantity": 100})

    # 3. Vendors
    vendor_syrup, _ = Vendor.objects.get_or_create(
        name="Global Syrup Corp",
        defaults={"email": "sales@globalsyrup.com", "category": "raw_material", "rating": 4.8}
    )
    vendor_pack, _ = Vendor.objects.get_or_create(
        name="EcoPack Solutions",
        defaults={"email": "orders@ecopack.com", "category": "packaging", "rating": 4.5}
    )

    # 4. Vendor Prices
    syrup_item = Item.objects.get(name="Fizz Classic Syrup (20L)")
    VendorPriceList.objects.get_or_create(
        vendor=vendor_syrup,
        item=syrup_item,
        defaults={"unit_price": Decimal("150.00"), "lead_time_days": 3}
    )

    # 5. Production Lines
    ProductionLine.objects.get_or_create(name="Fizz Bottling Line A", defaults={"location": "Main Facility"})
    ProductionLine.objects.get_or_create(name="Fizz Canning Line B", defaults={"location": "Main Facility"})

    print("✅ FreshFizz Seeding Complete!")

if __name__ == "__main__":
    seed_fizz_data()
