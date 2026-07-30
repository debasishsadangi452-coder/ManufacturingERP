"""Best-effort backfill of the new UnitOfMeasure FKs from the legacy free-text
`unit` string on items and BOM lines.

This is intentionally NOT a blind mapper. Free text like "unit", "bag", "cs"
won't all map cleanly, so the command:
  - auto-maps anything that matches a known unit code/alias,
  - defaults the rest to `each` and reports them as needing human review.

Run with --dry-run first to see what won't map, then re-run to apply. The
review screen in the UI reads the same "unmapped" set.
"""

from django.core.management.base import BaseCommand

from inventory.models import Item, BOMLine, UnitOfMeasure


# Common spreadsheet spellings → canonical unit code.
ALIASES = {
    "g": "g", "gram": "g", "grams": "g", "gm": "g",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg", "kilo": "kg",
    "each": "each", "ea": "each", "unit": "each", "units": "each", "pc": "each", "pcs": "each",
    "case": "case", "cs": "case", "cases": "case",
}


def _resolve(unit_text):
    """Return a canonical code for a free-text unit, or None if unrecognized."""
    if not unit_text:
        return None
    return ALIASES.get(unit_text.strip().lower())


class Command(BaseCommand):
    help = "Map legacy free-text units to UnitOfMeasure FKs on items and BOM lines."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; make no changes.")
        parser.add_argument(
            "--default-unmapped",
            action="store_true",
            help="Set unrecognized units to `each` (default: leave null for review).",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        default_unmapped = options["default_unmapped"]

        units = {u.code: u for u in UnitOfMeasure.objects.filter(company=None)}
        each = units.get("each")

        mapped = 0
        unmapped_items = []
        unmapped_lines = []

        for item in Item.objects.all():
            if item.base_unit_id:
                continue
            code = _resolve(item.unit)
            if code and code in units:
                if not dry:
                    item.base_unit = units[code]
                    item.purchase_unit = units[code]
                    item.save(update_fields=["base_unit", "purchase_unit"])
                mapped += 1
            else:
                unmapped_items.append((item.id, item.name, item.unit))
                if default_unmapped and not dry and each:
                    item.base_unit = each
                    item.purchase_unit = each
                    item.save(update_fields=["base_unit", "purchase_unit"])

        for line in BOMLine.objects.select_related("raw_material"):
            if line.unit_of_measure_id:
                continue
            code = _resolve(line.unit)
            if code and code in units:
                if not dry:
                    line.unit_of_measure = units[code]
                    line.save(update_fields=["unit_of_measure"])
                mapped += 1
            else:
                unmapped_lines.append((line.id, str(line.raw_material), line.unit))

        self.stdout.write(f"Auto-mapped: {mapped}")
        self.stdout.write(f"Items needing review: {len(unmapped_items)}")
        for iid, name, unit in unmapped_items[:50]:
            self.stdout.write(f"  item {iid}: {name!r} unit={unit!r}")
        self.stdout.write(f"BOM lines needing review: {len(unmapped_lines)}")
        for lid, name, unit in unmapped_lines[:50]:
            self.stdout.write(f"  bomline {lid}: {name} unit={unit!r}")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
        else:
            self.stdout.write(self.style.SUCCESS("Backfill applied."))
