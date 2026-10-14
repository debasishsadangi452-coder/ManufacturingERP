from django.db import migrations

# Legacy free-text unit -> shared unit code. Anything unrecognised is a count
# ("each"): bags, trays, boxes and the like are counted, not measured.
ALIASES = {
    "mg": "mg", "g": "g", "gm": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilogram": "kg", "kilograms": "kg",
    "t": "t", "tonne": "t", "tonnes": "t", "ton": "t",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "ml": "ml", "milliliter": "ml", "millilitre": "ml", "cl": "cl",
    "l": "l", "lt": "l", "ltr": "l", "liter": "l", "litre": "l", "liters": "l", "litres": "l",
    "gal": "gal", "gallon": "gal", "gallons": "gal",
    "qt": "qt", "quart": "qt", "pt": "pt", "pint": "pt",
    "fl oz": "fl_oz", "floz": "fl_oz", "cup": "cup", "cups": "cup",
    "dozen": "dozen", "doz": "dozen", "pair": "pair", "pairs": "pair",
    "case": "case", "cs": "case", "cases": "case",
}


def backfill(apps, schema_editor):
    Unit = apps.get_model("inventory", "UnitOfMeasure")
    Item = apps.get_model("inventory", "Item")
    BOMLine = apps.get_model("inventory", "BOMLine")
    units = {u.code: u for u in Unit.objects.filter(company=None)}
    each = units.get("each")
    if not units:
        return

    def resolve(text):
        return units.get(ALIASES.get((text or "").strip().lower(), "each")) or each

    for item in Item.objects.filter(base_unit__isnull=True):
        unit = resolve(item.unit)
        if unit:
            item.base_unit = unit
            if item.purchase_unit_id is None:
                item.purchase_unit = unit
            item.save(update_fields=["base_unit", "purchase_unit"])
    for line in BOMLine.objects.filter(unit_of_measure__isnull=True).select_related("raw_material"):
        # Same unit the material is stocked in, unless the line names another of the same kind.
        named = units.get(ALIASES.get((line.unit or "").strip().lower(), ""))
        base = line.raw_material.base_unit
        line.unit_of_measure = named if named and base and named.dimension == base.dimension else (base or named or each)
        line.save(update_fields=["unit_of_measure"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0028_seed_volume_and_extra_units"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
