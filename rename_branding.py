import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from inventory.models import Warehouse, Item
from production.models import ProductionLine
from workforce.models import Department

def rename_branding():
    print("🚀 Starting global branding update: Noodle -> Fizz")
    
    # 1. Update Warehouses
    warehouses = Warehouse.objects.filter(name__icontains='noodle')
    for w in warehouses:
        old_name = w.name
        w.name = w.name.replace('Noodle', 'Fizz').replace('noodle', 'fizz')
        w.save()
        print(f"✅ Renamed Warehouse: {old_name} -> {w.name}")

    # 2. Update Items
    items = Item.objects.filter(name__icontains='noodle')
    for i in items:
        old_name = i.name
        i.name = i.name.replace('Noodle', 'Fizz').replace('noodle', 'fizz')
        i.save()
        print(f"✅ Renamed Item: {old_name} -> {i.name}")

    # 3. Update Production Lines
    lines = ProductionLine.objects.filter(name__icontains='noodle')
    for l in lines:
        old_name = l.name
        l.name = l.name.replace('Noodle', 'Fizz').replace('noodle', 'fizz')
        l.save()
        print(f"✅ Renamed Production Line: {old_name} -> {l.name}")

    # 4. Update Departments
    depts = Department.objects.filter(description__icontains='noodle')
    for d in depts:
        old_desc = d.description
        d.description = d.description.replace('Noodle', 'Fizz').replace('noodle', 'fizz')
        d.save()
        print(f"✅ Updated Department Description: {old_desc} -> {d.description}")

    print("✨ Branding update complete!")

if __name__ == "__main__":
    rename_branding()
