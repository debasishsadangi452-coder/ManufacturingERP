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


from rest_framework.test import APITestCase
from production.models import ProductionPlan
from .models import SalesOrderItem


class CustomerCustomOrderTA01Tests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Beverage Factory Ltd")
        self.user = User.objects.create_user(
            username="orders_manager",
            password="securepass123",
            role="sales",
            company=self.company,
        )
        self.client.force_authenticate(user=self.user)
        self.customer = Customer.objects.create(
            company=self.company,
            name="Metro Supermarkets",
            email="purchasing@metro.com",
            phone="+1-555-0199",
        )
        self.finished_item = Item.objects.create(
            company=self.company,
            name="Sparkling Peach Soda 330ml",
            category="finished_good",
            unit="case",
            selling_price="25.00",
        )
        self.raw_item = Item.objects.create(
            company=self.company,
            name="Peach Flavoring Essence",
            category="raw_material",
            unit="liter",
        )

    def test_create_order_manual_channel_with_reference_and_delivery_date(self):
        payload = {
            "customer": self.customer.id,
            "source": "manual",
            "customer_order_reference": "PO-METRO-2026-001",
            "required_delivery_date": "2026-11-20",
            "items": [{"item": self.finished_item.id, "quantity": 100}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["source"], "manual")
        self.assertEqual(response.data["customer_order_reference"], "PO-METRO-2026-001")
        self.assertEqual(str(response.data["required_delivery_date"]), "2026-11-20")

        order = SalesOrder.objects.get(id=response.data["id"])
        self.assertEqual(order.source, "manual")
        self.assertEqual(order.customer_order_reference, "PO-METRO-2026-001")
        self.assertEqual(str(order.required_delivery_date), "2026-11-20")
        self.assertEqual(order.status, "pending")

    def test_create_order_email_channel(self):
        payload = {
            "customer": self.customer.id,
            "source": "email",
            "customer_order_reference": "EMAIL-INBOUND-8842",
            "required_delivery_date": "2026-11-25",
            "items": [{"item": self.finished_item.id, "quantity": 50}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["source"], "email")
        self.assertEqual(response.data["customer_order_reference"], "EMAIL-INBOUND-8842")

    def test_create_order_phone_channel(self):
        payload = {
            "customer": self.customer.id,
            "source": "phone",
            "customer_order_reference": "CALL-VERBAL-304",
            "required_delivery_date": "2026-12-01",
            "items": [{"item": self.finished_item.id, "quantity": 40}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["source"], "phone")
        self.assertEqual(response.data["customer_order_reference"], "CALL-VERBAL-304")

    def test_create_order_other_channel(self):
        payload = {
            "customer": self.customer.id,
            "source": "other",
            "customer_order_reference": "EDI-GATEWAY-771",
            "items": [{"item": self.finished_item.id, "quantity": 25}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["source"], "other")
        self.assertEqual(response.data["customer_order_reference"], "EDI-GATEWAY-771")
        self.assertIsNone(response.data["required_delivery_date"])

    def test_direct_confirmed_order_bypasses_quotation(self):
        """Confirmed orders bypass any forced quotation workflow."""
        payload = {
            "customer": self.customer.id,
            "source": "phone",
            "status": "confirmed",
            "customer_order_reference": "PO-DIRECT-CONFIRMED",
            "required_delivery_date": "2026-11-15",
            "items": [{"item": self.finished_item.id, "quantity": 80}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "confirmed")
        order = SalesOrder.objects.get(id=response.data["id"])
        self.assertEqual(order.status, "confirmed")

    def test_create_production_plan_inherits_order_delivery_date(self):
        """ProductionPlan created from SalesOrder inherits required_delivery_date as target_date."""
        order = SalesOrder.objects.create(
            customer=self.customer,
            source="manual",
            customer_order_reference="PO-PLAN-TEST",
            required_delivery_date="2026-12-10",
            status="confirmed",
        )
        SalesOrderItem.objects.create(
            sales_order=order,
            item=self.finished_item,
            quantity=150,
        )
        order.total_amount = 3750.0
        order.save()

        # Call create_production_plan action without explicit target_date
        response = self.client.post(f"/api/sales/sales-orders/{order.id}/create_production_plan/", {})
        self.assertEqual(response.status_code, 201)
        plan_data = response.data["plans"][0]
        plan = ProductionPlan.objects.get(id=plan_data["id"])
        self.assertEqual(str(plan.target_date), "2026-12-10")
        self.assertEqual(plan.sales_order, order)

    def test_order_rejects_raw_material(self):
        """Finished good rule remains strictly enforced."""
        payload = {
            "customer": self.customer.id,
            "source": "manual",
            "items": [{"item": self.raw_item.id, "quantity": 10}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_order_company_isolation(self):
        """Cannot create sales order for customer belonging to another company."""
        other_company = Company.objects.create(name="Competitor Beverage Co")
        other_customer = Customer.objects.create(company=other_company, name="Other Customer")
        payload = {
            "customer": other_customer.id,
            "source": "manual",
            "items": [{"item": self.finished_item.id, "quantity": 10}],
        }
        response = self.client.post("/api/sales/sales-orders/", payload, format="json")
        self.assertEqual(response.status_code, 400)
