"""Audit (and optionally resolve) legacy formulas that block migration 0020.

Migration ``production.0020_convert_existing_recipes_to_boms`` converts legacy
``Recipe`` rows into ``inventory.BOM`` rows, but it refuses to run when a product
owns more than one formula (it can't guess which is canonical) or when a formula
has no ingredients. The old ``Recipe`` schema has no version/active/date field,
so that ambiguity can only be resolved by a human.

This command surfaces exactly those records so you can decide, then applies the
decision. It is read-only unless you pass ``--apply``.

Typical workflow::

    # 1. See the conflicts, with each formula's ingredients side by side.
    python manage.py audit_legacy_formulas

    # 2. Resolve: keep one recipe per product, delete the rest; drop empties.
    python manage.py audit_legacy_formulas \
        --keep 5=24 --keep 51=32 --keep 52=13 --keep 53=29 \
        --keep 10=26 --keep 57=31 \
        --delete-empty \
        --apply

    # 3. Re-run the migration (now unambiguous).
    python manage.py migrate production 0020

``formula 27 for product 17 has no ingredients`` is handled by ``--delete-empty``
(remove it) or by adding ingredients to it by hand before migrating.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


# Kept in sync with migration 0020 so the audit mirrors what the migration checks.
SUPPORTED_PRODUCT_CATEGORIES = {"finished_good", "intermediate"}
SUPPORTED_COMPONENT_CATEGORIES = {
    "raw_material",
    "packaging",
    "intermediate",
    "semi_finished",
}


class Command(BaseCommand):
    help = (
        "Report legacy Recipe rows that block migration 0020 (products with "
        "multiple formulas, empty formulas) and optionally resolve them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            action="append",
            default=[],
            metavar="PRODUCT=RECIPE",
            help=(
                "For a product with multiple formulas, keep recipe RECIPE and "
                "delete the others. Repeatable, e.g. --keep 5=24 --keep 51=32."
            ),
        )
        parser.add_argument(
            "--keep-original",
            action="store_true",
            help=(
                "For every product with multiple formulas, keep the ORIGINAL "
                "formula and delete the later duplicates. 'Original' means the "
                "lowest-id formula that actually has ingredients (an empty "
                "lowest-id row is skipped, since it can't become a usable BOM). "
                "This matches the formula->BOM rename: only the original formula "
                "is retained as the BOM. Overridden per product by --keep. "
                "A product whose formulas are ALL empty can't be resolved this "
                "way and is reported for a manual decision."
            ),
        )
        parser.add_argument(
            "--delete-empty",
            action="store_true",
            help="Delete formulas that have no ingredients.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Actually perform deletions. Without this flag the command only "
                "reports what it would do (dry run)."
            ),
        )

    def handle(self, *args, **options):
        from production.models import Recipe

        keep_map = self._parse_keep(options["keep"])
        keep_original = options["keep_original"]
        delete_empty = options["delete_empty"]
        apply_changes = options["apply"]

        recipes = list(
            Recipe.objects.select_related("product")
            .prefetch_related("recipeingredient_set__item")
            .order_by("id")
        )

        # Exclude products that already have a BOM: migration 0020 skips those,
        # so they are not a source of conflict.
        from inventory.models import BOM

        already_converted = set(
            BOM.objects.values_list("finished_good_id", flat=True)
        )

        groups = defaultdict(list)
        for recipe in recipes:
            if recipe.product_id in already_converted:
                continue
            groups[recipe.product_id].append(recipe)

        multi = {pid: rs for pid, rs in groups.items() if len(rs) > 1}
        empties = [
            r
            for rs in groups.values()
            for r in rs
            if not r.recipeingredient_set.all()
        ]

        self._report(multi, empties)

        if not multi and not empties:
            self.stdout.write(self.style.SUCCESS(
                "No blocking formulas found. Migration 0020 should run cleanly."
            ))
            return

        # Work out which recipes to delete.
        to_delete = []  # list of (recipe, reason)
        unresolved_products = []

        for pid, rs in multi.items():
            if pid not in keep_map:
                if keep_original:
                    # The original = the lowest-id formula that actually has
                    # ingredients. A lower-id empty row is a dud from the rename
                    # and would leave the migration failing on "no ingredients".
                    non_empty = [r for r in rs if r.recipeingredient_set.all()]
                    if not non_empty:
                        # Every formula is empty — nothing valid to keep.
                        unresolved_products.append(pid)
                        continue
                    keep_map[pid] = min(r.pk for r in non_empty)
                else:
                    unresolved_products.append(pid)
                    continue
            keep_id = keep_map[pid]
            recipe_ids = {r.pk for r in rs}
            if keep_id not in recipe_ids:
                raise CommandError(
                    f"--keep {pid}={keep_id}: recipe {keep_id} is not one of "
                    f"product {pid}'s formulas ({sorted(recipe_ids)})."
                )
            for r in rs:
                if r.pk != keep_id:
                    to_delete.append((r, f"not kept for product {pid} (kept {keep_id})"))

        if delete_empty:
            already = {id(r) for r, _ in to_delete}
            for r in empties:
                if id(r) not in already:
                    to_delete.append((r, "empty formula (--delete-empty)"))

        # Report the plan.
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Resolution plan"))
        if unresolved_products:
            self.stdout.write(self.style.WARNING(
                "  Products with multiple formulas still needing a --keep decision: "
                + ", ".join(str(p) for p in sorted(unresolved_products))
            ))
        if to_delete:
            for r, reason in to_delete:
                self.stdout.write(f"  DELETE recipe {r.pk} (product {r.product_id}) — {reason}")
        else:
            self.stdout.write("  Nothing to delete with the given options.")

        if not to_delete:
            return

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.NOTICE(
                "Dry run — no changes made. Re-run with --apply to delete the "
                "recipes listed above."
            ))
            return

        # Apply.
        from production.models import Recipe as RecipeModel

        delete_ids = [r.pk for r, _ in to_delete]
        with transaction.atomic():
            deleted, _ = RecipeModel.objects.filter(pk__in=delete_ids).delete()
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {len(delete_ids)} recipe row(s) (cascade total {deleted} objects)."
        ))
        # A product is still a problem only if, after the deletions above, it
        # would still leave 0020 unable to proceed: either 2+ formulas survive
        # (can't pick one) or exactly one survives but it is empty.
        deleted_set = set(delete_ids)
        remaining_conflicts = []
        for pid, rs in multi.items():
            survivors = [r for r in rs if r.pk not in deleted_set]
            if len(survivors) > 1:
                remaining_conflicts.append(pid)
            elif len(survivors) == 1 and not survivors[0].recipeingredient_set.all():
                remaining_conflicts.append(pid)
        if remaining_conflicts:
            self.stdout.write(self.style.WARNING(
                "Still unresolved: products "
                + ", ".join(str(p) for p in sorted(remaining_conflicts))
                + ". Migration 0020 will still fail for these."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "All multi-formula conflicts resolved. Re-run migration 0020."
            ))

    # -- helpers ---------------------------------------------------------

    def _parse_keep(self, raw_values):
        keep_map = {}
        for raw in raw_values:
            if "=" not in raw:
                raise CommandError(f"--keep expects PRODUCT=RECIPE, got {raw!r}.")
            prod_s, recipe_s = raw.split("=", 1)
            try:
                prod_id = int(prod_s)
                recipe_id = int(recipe_s)
            except ValueError:
                raise CommandError(f"--keep expects integer ids, got {raw!r}.")
            if prod_id in keep_map and keep_map[prod_id] != recipe_id:
                raise CommandError(
                    f"--keep given twice for product {prod_id} with different "
                    f"recipes ({keep_map[prod_id]} and {recipe_id})."
                )
            keep_map[prod_id] = recipe_id
        return keep_map

    def _report(self, multi, empties):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Products with multiple formulas"
        ))
        if not multi:
            self.stdout.write("  (none)")
        for pid in sorted(multi):
            recipes = multi[pid]
            product = recipes[0].product
            self.stdout.write(
                f"\n  Product {pid}: {product.name!r} "
                f"[category={product.category}] — {len(recipes)} formulas"
            )
            if product.category not in SUPPORTED_PRODUCT_CATEGORIES:
                self.stdout.write(self.style.WARNING(
                    f"    ! product category {product.category!r} is unsupported "
                    "by migration 0020"
                ))
            for r in recipes:
                ings = list(r.recipeingredient_set.all())
                self.stdout.write(
                    f"    recipe {r.pk}: batch_size={r.batch_size}, "
                    f"{len(ings)} ingredient(s)"
                )
                for ing in ings:
                    flag = ""
                    if ing.item.category not in SUPPORTED_COMPONENT_CATEGORIES:
                        flag += f" [bad category {ing.item.category!r}]"
                    if ing.quantity is not None and ing.quantity <= 0:
                        flag += " [non-positive qty]"
                    self.stdout.write(
                        f"        - item {ing.item_id} {ing.item.name!r}: "
                        f"qty={ing.quantity}{flag}"
                    )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Empty formulas"))
        if not empties:
            self.stdout.write("  (none)")
        for r in empties:
            self.stdout.write(
                f"  recipe {r.pk} for product {r.product_id} "
                f"({r.product.name!r}) has no ingredients"
            )
