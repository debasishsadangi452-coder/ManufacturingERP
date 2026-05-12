import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from maintenance.models import Equipment
from production.models import ProductionLine

def setup_maintenance():
    print("Setting up maintenance equipment...")
    
    lines = ProductionLine.objects.all()
    if not lines.exists():
        print("No production lines found. Please run setup_production_lines.py first.")
        return

    equipment_data = [
        {"name": "Bottling Machine Alpha", "line_name": "Production Line A"},
        {"name": "Capping Unit Alpha", "line_name": "Production Line A"},
        {"name": "Labeling Machine Beta", "line_name": "Production Line B"},
        {"name": "Carbonation Tank Beta", "line_name": "Production Line B"},
        {"name": "Mixing Vessel Gamma", "line_name": "Production Line C"},
        {"name": "Filling Station Delta", "line_name": "Production Line D"},
    ]

    for data in equipment_data:
        try:
            line = ProductionLine.objects.get(name=data["line_name"])
            equipment, created = Equipment.objects.get_or_create(
                name=data["name"],
                line=line,
                defaults={"status": "running", "health": 100}
            )
            if created:
                print(f"Created equipment: {data['name']} on {data['line_name']}")
            else:
                print(f"Equipment already exists: {data['name']}")
        except ProductionLine.DoesNotExist:
            print(f"Production Line {data['line_name']} does not exist.")

    print("Maintenance setup complete.")

if __name__ == "__main__":
    setup_maintenance()
