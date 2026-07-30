"""Fill in the catalogue fields that make an Item a usable catalogue entry.

Items created by the demo seed and by early onboarding carry only a name,
category and cost — no SKU, no reorder point, no ERP classification. That is
enough to transact against but not enough to *look up*: you cannot scan a code,
sort a purchasing list, or see what is about to run out.

This command derives those fields from data already in the system rather than
inventing them:

  sku                 built from category + name (see `_make_sku`), unique
                      within the company.
  erp_classification  mirrors `category`, which is the same distinction the
                      onboarding wizard asks a human to confirm.
  reorder_point       for raw materials, the quantity consumed by
                      `--cover-batches` worth of production (default 5), read
                      off the actual recipes. Items in no recipe are skipped
                      rather than given a made-up number.

Scope it with --company to avoid touching other tenants. Dry-run by default:

    python manage.py backfill_catalogue --company 26
    python manage.py backfill_catalogue --company 26 --apply
"""

import re

from django.core.management.base import BaseCommand, CommandError

from inventory.models import Item
from production.models import Recipe, RecipeIngredient


# Short prefixes so a SKU says what kind of thing it is at a glance.
CATEGORY_PREFIX = {
    "raw_material": "RM",
    "finished_good": "FG",
}


def _make_sku(item, taken):
    """Build a readable, stable SKU: RM-CANE-SUGAR, FG-COOKIES-CREAM.

    Derived from the name so it stays meaningful to a human reading a
    purchase order. `taken` guards uniqueness within the company.
    """
    prefix = CATEGORY_PREFIX.get(item.category, "IT")
    # Drop punctuation ("Cookies & Cream" → "COOKIES CREAM"), then hyphenate.
    words = re.sub(r"[^A-Za-z0-9\s]", " ", item.name).split()
    slug = "-".join(w.upper() for w in words[:3]) or f"ITEM{item.id}"
    base = f"{prefix}-{slug}"

    sku = base
    n = 2
    while sku in taken:
        sku = f"{base}-{n}"
        n += 1
    taken.add(sku)
    return sku


def _usage_per_batch(company):
    """Map item_id → quantity consumed per batch, summed across recipes.

    RecipeIngredient.quantity is per batch (see Recipe.batch_size), so this is
    already the right unit to scale a reorder point from.
    """
    usage = {}
    recipes = Recipe.objects.filter(product__company=company)
    for ing in RecipeIngredient.objects.filter(recipe__in=recipes).select_related("item"):
        usage[ing.item_id] = usage.get(ing.item_id, 0) + ing.quantity
    return usage


class Command(BaseCommand):
    help = "Populate SKU, ERP classification and reorder points on a company's items."

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, required=True, help="Company id to backfill.")
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
        parser.add_argument(
            "--cover-batches", type=int, default=5,
            help="Reorder point = raw material used by this many batches (default 5).",
        )
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Also replace values that are already set (default: only fill blanks).",
        )

    def handle(self, *args, **options):
        company_id = options["company"]
        apply_changes = options["apply"]
        cover = options["cover_batches"]
        overwrite = options["overwrite"]

        items = list(Item.objects.filter(company_id=company_id).order_by("category", "name"))
        if not items:
            raise CommandError(f"No items found for company {company_id}.")

        company = items[0].company
        usage = _usage_per_batch(company)

        # SKUs already in use for this company, so we never collide.
        taken = {i.sku for i in items if i.sku}

        changed = 0
        skipped_reorder = []

        for item in items:
            updates = {}

            if overwrite or not item.sku:
                if overwrite and item.sku:
                    taken.discard(item.sku)
                updates["sku"] = _make_sku(item, taken)

            if overwrite or not item.erp_classification:
                updates["erp_classification"] = item.category

            # Only raw materials get a consumption-derived reorder point;
            # finished goods are driven by sales demand, not recipes.
            if item.category == "raw_material" and (overwrite or item.reorder_point is None):
                per_batch = usage.get(item.id)
                if per_batch:
                    updates["reorder_point"] = round(per_batch * cover, 2)
                else:
                    # No recipe uses it — we have no basis for a number.
                    skipped_reorder.append(item.name)

            if not updates:
                continue

            changed += 1
            detail = ", ".join(f"{k}={v!r}" for k, v in updates.items())
            self.stdout.write(f"  {item.name:28} {detail}")

            if apply_changes:
                for field, value in updates.items():
                    setattr(item, field, value)
                item.save(update_fields=list(updates))

        self.stdout.write("")
        self.stdout.write(f"Company: {company} (id={company_id})")
        self.stdout.write(f"Items examined: {len(items)}  |  updated: {changed}")

        if skipped_reorder:
            self.stdout.write(self.style.WARNING(
                "No reorder point set (not used by any recipe): " + ", ".join(skipped_reorder)
            ))

        if apply_changes:
            self.stdout.write(self.style.SUCCESS("Catalogue backfill applied."))
        else:
            self.stdout.write(self.style.WARNING("Dry run — no changes written. Re-run with --apply."))
