from django.db import migrations

def create_default_lines(apps, schema_editor):
    ProductionLine = apps.get_model('production', 'ProductionLine')
    lines = [
        {"name": "Production Line 1", "location": "Main Facility - Section A", "capacity": 150.0},
        {"name": "Production Line 2", "location": "Main Facility - Section B", "capacity": 150.0},
        {"name": "Production Line 3", "location": "North Wing", "capacity": 100.0},
        {"name": "Production Line 4", "location": "Specialty Unit", "capacity": 50.0},
    ]
    for line in lines:
        ProductionLine.objects.get_or_create(name=line["name"], defaults={
            "location": line["location"],
            "capacity": line["capacity"],
            "is_active": True
        })

class Migration(migrations.Migration):

    dependencies = [
        ('production', '0003_productionline_capacity_productionline_location'),
    ]

    operations = [
        migrations.RunPython(create_default_lines),
    ]
