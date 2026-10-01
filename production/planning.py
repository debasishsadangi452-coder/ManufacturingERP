"""Production planning helpers: material checks that understand intermediates
(multi-level BOMs) and the default production line for a recipe."""
from django.db.models import Sum

from inventory.models import Stock


def default_line_for(recipe):
    """The recipe's predefined line, when it can take work right now."""
    line = recipe.default_line
    if line and line.is_active and line.status != "maintenance":
        return line
    return None


def _on_hand(item):
    return Stock.objects.filter(item=item).aggregate(total=Sum("quantity"))["total"] or 0


def _qty(value):
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _amount_of(value, item):
    """"7 kg of Flour", or "5,760 Baked Cookie" for count units."""
    if (item.unit or "").lower() in ("", "each", "unit", "units", "pcs"):
        return f"{_qty(value)} {item.name}"
    return f"{_qty(value)} {item.unit} of {item.name}"


def material_check(recipe, quantity, _depth=0):
    """Compare what `quantity` units of the recipe's product needs with stock.

    Returns {"rows": [...], "warnings": [...], "intermediate_warnings": [...],
    "can_produce": bool}. An intermediate that is short (e.g. baked cookies for
    a packing run) gets a warning saying how many to make first and how many
    batches that is, plus any raw-material shortage for making them.
    """
    rows, warnings, intermediate_warnings = [], [], []
    for ing, required in recipe.material_requirements(quantity):
        item = ing.item
        available = _on_hand(item)
        short = max(required - available, 0)
        row = {
            "item_id": item.id,
            "item_name": item.name,
            "category": item.category,
            "unit": item.unit,
            "required": required,
            "available": available,
            "short": short,
        }
        rows.append(row)
        if short <= 0:
            continue

        if item.category == "intermediate":
            sub_recipe = item.recipes.first()
            if not sub_recipe:
                message = (f"Needs {_amount_of(required, item)} (intermediate) but only "
                           f"{_qty(available)} in stock, and {item.name} has no recipe to make it.")
            else:
                plan = sub_recipe.batches_for(short)
                row["make"] = {"recipe_id": sub_recipe.id, "batches": plan["batches"],
                               "units": plan["units_produced"]}
                message = (f"Needs {_amount_of(required, item)} (intermediate) but only "
                           f"{_qty(available)} in stock. Produce {_qty(short)} more {item.name} first: "
                           f"{plan['batches']} batch(es) of {_qty(sub_recipe.batch_size or 1)}.")
                if _depth < 3:
                    nested = material_check(sub_recipe, short, _depth + 1)
                    for nested_warning in nested["warnings"]:
                        message += f" To make it: {nested_warning}"
            intermediate_warnings.append(message)
            warnings.append(message)
        else:
            warnings.append(f"Short {_amount_of(short, item)} "
                            f"(needs {_qty(required)}, {_qty(available)} in stock).")

    return {
        "rows": rows,
        "warnings": warnings,
        "intermediate_warnings": intermediate_warnings,
        "can_produce": not warnings,
    }
