"""Unit-of-measure conversion.

Single entry point `convert(qty, from_uom, to_uom)`. Conversions are only valid
within a dimension — converting grams to "each" is a programming error and
raises, rather than silently producing a wrong number that corrupts stock.

Standard seed units (also used by the data migration):
    mass:  g (base), oz, lb, kg
    count: each (base), case
Volume is defined for future use but not seeded with cross-unit factors.
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


def convert(qty, from_uom, to_uom):
    """Convert `qty` from one UnitOfMeasure to another within the same dimension.

    `from_uom`/`to_uom` are UnitOfMeasure instances. Returns a Decimal.
    Raises UomConversionError if the dimensions differ, so callers can never
    accidentally reconcile mass against count.
    """
    if from_uom is None or to_uom is None:
        raise UomConversionError("Both source and target units are required to convert.")
    if from_uom.pk == to_uom.pk:
        return Decimal(str(qty))
    if from_uom.dimension != to_uom.dimension:
        raise UomConversionError(
            f"Cannot convert {from_uom.code} ({from_uom.dimension}) to "
            f"{to_uom.code} ({to_uom.dimension}) — different dimensions."
        )
    base = Decimal(str(qty)) * Decimal(str(from_uom.to_base_factor))
    return base / Decimal(str(to_uom.to_base_factor))
