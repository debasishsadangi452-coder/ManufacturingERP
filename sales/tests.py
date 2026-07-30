from django.test import TestCase

from accounts.models import Company, User
from inventory.models import Item

from .models import Customer, SalesOrder
from .serializers import SalesOrderSerializer


class SerializerRequest:
    def __init__(self, data, user):
        self.data = data
        self.user = user


class SalesOrderItemValidationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Demo Plant")
        self.user = User.objects.create_user(
            username="sales_user",
            password="pass123",
            role="sales",
            company=self.company,
        )
        self.customer = Customer.objects.create(company=self.company, name="Retail Chain")
        self.raw_item = Item.objects.create(
            company=self.company,
            name="Cane Sugar",
            category="raw_material",
            unit="kg",
        )
        self.finished_item = Item.objects.create(
            company=self.company,
            name="Sparkling Water",
            category="finished_good",
            unit="bottle",
            selling_price="20.00",
        )

    def _request(self, item):
        return SerializerRequest(
            {"items": [{"item": item.id, "quantity": 5}]},
            self.user,
        )

    def test_sales_order_rejects_raw_materials(self):
        serializer = SalesOrderSerializer(
            data={"customer": self.customer.id},
            context={"request": self._request(self.raw_item)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        with self.assertRaisesMessage(Exception, "Only finished goods can be sold"):
            serializer.save()

        self.assertFalse(SalesOrder.objects.exists())

    def test_sales_order_accepts_finished_goods(self):
        serializer = SalesOrderSerializer(
            data={"customer": self.customer.id},
            context={"request": self._request(self.finished_item)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        order = serializer.save()

        self.assertEqual(order.salesorderitem_set.count(), 1)
        self.assertEqual(order.total_amount, 100)


class EmailToDraftOrderTests(TestCase):
    """P0-B: email → draft order pipeline, matching, and the QB guardrail."""

    def setUp(self):
        from unittest import mock
        self.company = Company.objects.create(name="Red Velvet NYC")
        self.customer = Customer.objects.create(company=self.company, name="Costco NE")
        self.cookie = Item.objects.create(
            company=self.company, name="Cookies & Cream", category="finished_good",
            selling_price=10, unit="case",
        )

    def _parse(self, **over):
        """A fake extraction result, so tests don't call the LLM."""
        base = {"customer": "Costco NE", "pickup_date": "2026-09-15",
                "lines": [{"product": "Cookies & Cream", "cases": 200}], "confidence": 0.98}
        base.update(over)
        return base

    def test_clean_email_creates_draft_order(self):
        from unittest import mock
        from sales.email_orders import create_draft_from_email

        with mock.patch("sales.email_orders.extract_order", return_value=self._parse()):
            inbound = create_draft_from_email(
                self.company, "buyer@costco.com", "PO 500 cases", "body text"
            )
        self.assertEqual(inbound.status, "parsed")
        self.assertIsNotNone(inbound.sales_order)
        order = inbound.sales_order
        self.assertEqual(order.status, "draft")
        self.assertEqual(order.source, "email")
        self.assertEqual(order.salesorderitem_set.first().quantity, 200)

    def test_low_confidence_flags_needs_attention(self):
        from unittest import mock
        from sales.email_orders import create_draft_from_email

        with mock.patch("sales.email_orders.extract_order",
                        return_value=self._parse(confidence=0.5)):
            inbound = create_draft_from_email(self.company, "x@y.com", "sub", "body")
        self.assertEqual(inbound.status, "needs_attention")

    def test_unmatched_product_flags_needs_attention(self):
        from unittest import mock
        from sales.email_orders import create_draft_from_email

        with mock.patch(
            "sales.email_orders.extract_order",
            return_value=self._parse(lines=[{"product": "Unknown Widget", "cases": 5}]),
        ):
            inbound = create_draft_from_email(self.company, "x@y.com", "sub", "body")
        self.assertEqual(inbound.status, "needs_attention")
        self.assertIn("Unmatched", inbound.error_message)

    def test_draft_order_does_not_push_to_quickbooks(self):
        """The guardrail: a draft order must never queue a QuickBooks push."""
        from unittest import mock
        from sales.email_orders import create_draft_from_email

        with mock.patch("sales.email_orders.extract_order", return_value=self._parse()), \
             mock.patch("quickbooks.signals._queue_push") as queue_push:
            create_draft_from_email(self.company, "buyer@costco.com", "sub", "body")
        # No push queued while the order sits in draft.
        for call in queue_push.call_args_list:
            self.assertNotEqual(call.args[0], "sales_order")

    def test_confirm_promotes_order_out_of_draft(self):
        from unittest import mock
        from sales.email_orders import create_draft_from_email

        with mock.patch("sales.email_orders.extract_order", return_value=self._parse()):
            inbound = create_draft_from_email(self.company, "buyer@costco.com", "sub", "body")
        order = inbound.sales_order
        order.status = "pending"
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")
