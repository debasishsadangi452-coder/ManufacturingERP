from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from documents.builders import customer_order_confirmation_doc


class _Lines:
    def __init__(self, lines):
        self.lines = lines

    def select_related(self, *_args):
        return self

    def all(self):
        return self.lines


class CustomerOrderConfirmationTests(TestCase):
    def test_float_quantity_and_decimal_price_render_line_amount(self):
        line = SimpleNamespace(
            item=SimpleNamespace(sku="FG-1", name="Finished good", unit="Each"),
            quantity=2.5,
            unit_price=Decimal("4.00"),
        )
        order = SimpleNamespace(
            customer=SimpleNamespace(
                company=SimpleNamespace(name="Example Co"),
                name="Example Customer",
                email="",
                phone="",
            ),
            id=12,
            get_status_display=lambda: "Confirmed",
            created_at=None,
            required_delivery_date=None,
            customer_order_reference="",
            get_source_display=lambda: "Manual entry",
            get_priority_display=lambda: "Normal",
            delivery_requirements="",
            salesorderitem_set=_Lines([line]),
            total_amount=Decimal("10.00"),
            custom_specifications="",
        )

        document = customer_order_confirmation_doc(order)

        self.assertEqual(document["tables"][0]["rows"][0][-1]["value"], "10.00")
