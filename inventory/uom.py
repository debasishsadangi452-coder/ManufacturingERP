"""Unit-of-measure conversion.

Single entry point `convert(qty, from_uom, to_uom)`. Conversions are only valid
within a dimension — converting grams to "each" is a programming error and
raises, rather than silently producing a wrong number that corrupts stock.

Standard seed units (also used by the data migrations):
    mass:   g (base), mg, oz, lb, kg, t
    volume: ml (base), cl, l, fl_oz, cup, pt, qt, gal   (US customary)
    count:  each (base), case, dozen, pair
"""

from decimal import Decimal


class UomConversionError(ValueError):
    """Raised when two units cannot be converted (different dimensions)."""


# The standard units seeded company-wide. (code, name, dimension, to_base, is_base)
STANDARD_UNITS = [
    ("g", "Gram", "mass", Decimal("1"), True),
    ("oz", "Ounce", "mass", Decimal("28.34952312"), False),
    ("lb", "Pound", "mass", Decimal("453.59237"), False),
    ("kg", "Kilogram", "mass", Decimal("1000"), False),
    ("each", "Each", "count", Decimal("1"), True),
    ("case", "Case", "count", Decimal("1"), False),  # per-item case size overrides this
]

# Added after the first release; seeded by inventory migration 0028.
EXTRA_UNITS = [
    ("mg", "Milligram", "mass", Decimal("0.001"), False),
    ("t", "Tonne (metric)", "mass", Decimal("1000000"), False),
    ("ml", "Millilitre", "volume", Decimal("1"), True),
    ("cl", "Centilitre", "volume", Decimal("10"), False),
    ("l", "Litre", "volume", Decimal("1000"), False),
    ("fl_oz", "Fluid ounce (US)", "volume", Decimal("29.5735295625"), False),
    ("cup", "Cup (US)", "volume", Decimal("236.5882365"), False),
    ("pt", "Pint (US)", "volume", Decimal("473.176473"), False),
    ("qt", "Quart (US)", "volume", Decimal("946.352946"), False),
    ("gal", "Gallon (US)", "volume", Decimal("3785.411784"), False),
    ("dozen", "Dozen", "count", Decimal("12"), False),
    ("pair", "Pair", "count", Decimal("2"), False),
]


def convert_by_code(qty, from_code, to_code, company=None):
    """Convert using unit codes (shared units, or a company's own)."""
    from django.db.models import Q
    from .models import UnitOfMeasure
    units = {u.code: u for u in UnitOfMeasure.objects.filter(Q(company=None) | Q(company=company))}
    try:
        return convert(qty, units[from_code], units[to_code])
    except KeyError as missing:
        raise UomConversionError(f"Unknown unit {missing}.")


def convert(qty, from_uom, to_uom, item=None):
    """Convert `qty` from one UnitOfMeasure to another within the same dimension.

    `from_uom`/`to_uom` are UnitOfMeasure instances. Returns a Decimal.
    Raises UomConversionError if the dimensions differ, so callers can never
    accidentally reconcile mass against count.
    """
    if from_uom is None or to_uom is None:
        raise UomConversionError("Both source and target units are required to convert.")
    if from_uom.pk == to_uom.pk:
        return Decimal(str(qty))
    if item is not None:
        from .models import ItemUOMConversion
        conversion = ItemUOMConversion.objects.filter(
            item=item, from_unit=from_uom, to_unit=to_uom
        ).first()
        if conversion:
            return Decimal(str(qty)) * conversion.factor
        reverse = ItemUOMConversion.objects.filter(
            item=item, from_unit=to_uom, to_unit=from_uom
        ).first()
        if reverse:
            return Decimal(str(qty)) / reverse.factor
    if from_uom.dimension != to_uom.dimension:
        if item is None:
            raise UomConversionError(
                f"Cannot convert {from_uom.code} ({from_uom.dimension}) to "
                f"{to_uom.code} ({to_uom.dimension}) without an item-specific conversion."
            )
        raise UomConversionError(
            f"No item-specific conversion exists for {item.name}: "
            f"{from_uom.code} to {to_uom.code}."
        )
    base = Decimal(str(qty)) * Decimal(str(from_uom.to_base_factor))
    return base / Decimal(str(to_uom.to_base_factor))
