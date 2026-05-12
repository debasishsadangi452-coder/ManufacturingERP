import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from production.models import ProductionLine

lines = [
    {"name": "Production Line A", "location": "Hall 1", "capacity": 500, "is_active": True},
    {"name": "Production Line B", "location": "Hall 1", "capacity": 500, "is_active": True},
    {"name": "Production Line C", "location": "Hall 2", "capacity": 750, "is_active": True},
    {"name": "Production Line D", "location": "Hall 2", "capacity": 750, "is_active": True},
]

for line_data in lines:
    obj, created = ProductionLine.objects.get_or_create(
        name=line_data["name"],
        defaults=line_data
    )
    if created:
        print(f"Created {obj.name}")
    else:
        print(f"{obj.name} already exists")

print("Production lines setup complete.")
