from collections import Counter, defaultdict

from django.db import migrations


SUPPORTED_PRODUCT_CATEGORIES = {"finished_good", "intermediate"}
SUPPORTED_COMPONENT_CATEGORIES = {
    "raw_material",
    "packaging",
    "intermediate",
    "semi_finished",
}


def convert_recipes_to_boms(apps, schema_editor):
    Recipe = apps.get_model("production", "Recipe")
    RecipeIngredient = apps.get_model("production", "RecipeIngredient")
    BOM = apps.get_model("inventory", "BOM")
    BOMLine = apps.get_model("inventory", "BOMLine")
    database = schema_editor.connection.alias

    existing_bom_product_ids = set(
        BOM.objects.using(database).values_list("finished_good_id", flat=True)
    )
    recipe_groups = defaultdict(list)
    recipes = list(
        Recipe.objects.using(database)
        .select_related("product")
        .prefetch_related("recipeingredient_set__item")
        .order_by("id")
    )
    for recipe in recipes:
        if recipe.product_id not in existing_bom_product_ids:
            recipe_groups[recipe.product_id].append(recipe)

    errors = []
    for product_id, product_recipes in recipe_groups.items():
        if len(product_recipes) > 1:
            errors.append(
                f"product {product_id} has multiple formulas "
                f"({', '.join(str(recipe.pk) for recipe in product_recipes)})"
            )
            continue

        recipe = product_recipes[0]
        product = recipe.product
        if product.category not in SUPPORTED_PRODUCT_CATEGORIES:
            errors.append(
                f"formula {recipe.pk} product {product_id} has unsupported "
                f"category {product.category!r}"
            )
        ingredients = list(recipe.recipeingredient_set.all())
        if not ingredients:
            errors.append(f"formula {recipe.pk} for product {product_id} has no ingredients")
        for ingredient in ingredients:
            if ingredient.item.category not in SUPPORTED_COMPONENT_CATEGORIES:
                errors.append(
                    f"formula {recipe.pk} component {ingredient.item_id} has "
                    f"unsupported category {ingredient.item.category!r}"
                )
            if ingredient.quantity <= 0:
                errors.append(
                    f"formula {recipe.pk} component {ingredient.item_id} "
                    "has a non-positive quantity"
                )
        if recipe.batch_size is not None and recipe.batch_size < 0:
            errors.append(f"formula {recipe.pk} has a negative batch size")

    if errors:
        raise RuntimeError(
            "Cannot convert all legacy formulas to BOMs. Resolve these records and "
            "retry the migration: " + "; ".join(errors)
        )

    for product_id, product_recipes in recipe_groups.items():
        if not product_recipes:
            continue
        recipe = product_recipes[0]
        bom = BOM.objects.using(database).create(
            finished_good_id=product_id,
            is_active=True,
            status="active",
            version="1.0",
        )
        batch_size = recipe.batch_size or 1
        quantities_by_component = Counter()
        component_items = {}
        for ingredient in recipe.recipeingredient_set.all():
            quantities_by_component[ingredient.item_id] += ingredient.quantity / batch_size
            component_items[ingredient.item_id] = ingredient.item

        BOMLine.objects.using(database).bulk_create([
            BOMLine(
                bom_id=bom.pk,
                raw_material_id=item_id,
                quantity=quantity,
                unit=component_items[item_id].unit or "unit",
            )
            for item_id, quantity in quantities_by_component.items()
        ])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0024_item_costing_rule_item_default_warehouse_and_more"),
        ("production", "0019_manufacturingsettings_business_rules_confirmed_and_more"),
    ]

    operations = [
        migrations.RunPython(convert_recipes_to_boms, migrations.RunPython.noop),
    ]
