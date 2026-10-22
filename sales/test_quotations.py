"""Quotations: pricing, lifecycle, and conversion into a sales order."""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item
from sales.models import Customer, Quotation, SalesOrder

URL = "/api/sales/quotations/"


class QuotationTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
        self.user = User.objects.create_user(
            username="sam.sales", email="sam@fizz.test", role="sales", company=self.company, password="pass",
        )
        self.customer = Customer.objects.create(company=self.company, name="Corner Shop")
        self.soda = Item.objects.create(company=self.company, name="Soda", category="finished_good",
                                        selling_price=Decimal("5.00"))
        self.client.force_authenticate(user=self.user)

    def _create(self, **extra):
        body = {"customer": self.customer.id, "items": [{"item": self.soda.id, "quantity": 10}], **extra}
        return self.client.post(URL, body, format="json")

    def test_create_prices_from_catalog_by_default(self):
        res = self._create()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertTrue(res.data["quote_number"].startswith("QUO-"))
        self.assertEqual(res.data["total_amount"], "50.00")
        self.assertEqual(res.data["items"][0]["unit_price"], "5.00")

    def test_quoted_price_discount_and_tax(self):
        res = self._create(items=[{"item": self.soda.id, "quantity": 10, "unit_price": "4"}],
                           discount_percent=10, tax_amount="2")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        # 40 - 10% + 2
        self.assertEqual(res.data["total_amount"], "38.00")
        self.assertEqual(res.data["items"][0]["list_price"], "5.00")

    def test_rejects_raw_materials_and_foreign_customers(self):
        sugar = Item.objects.create(company=self.company, name="Sugar", category="raw_material")
        self.assertEqual(self._create(items=[{"item": sugar.id, "quantity": 1}]).status_code, 400)
        other = Customer.objects.create(company=Company.objects.create(name="Other", slug="other"), name="X")
        self.assertEqual(self._create(customer=other.id).status_code, 400)

    def test_convert_keeps_quoted_price_by_default(self):
        q = self._create(items=[{"item": self.soda.id, "quantity": 10, "unit_price": "4"}], discount_percent=10).data
        res = self.client.post(f"{URL}{q['id']}/convert/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        order = SalesOrder.objects.get(pk=res.data["sales_order"]["id"])
        self.assertEqual(order.salesorderitem_set.get().unit_price, Decimal("4.00"))
        self.assertEqual(order.total_amount, Decimal("36.00"))
        self.assertEqual(order.discount_amount, Decimal("4.00"))
        self.assertEqual(order.status, "pending")
        quotation = Quotation.objects.get(pk=q["id"])
        self.assertEqual((quotation.status, quotation.converted_order_id), ("converted", order.id))

    def test_convert_with_price_change(self):
        q = self._create().data
        line = q["items"][0]["id"]
        res = self.client.post(f"{URL}{q['id']}/convert/", {"prices": {str(line): "6.5"}}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        order = SalesOrder.objects.get(pk=res.data["sales_order"]["id"])
        self.assertEqual(order.salesorderitem_set.get().unit_price, Decimal("6.50"))
        self.assertEqual(order.total_amount, Decimal("65.00"))

    def test_cannot_convert_twice_or_edit_after_converting(self):
        q = self._create().data
        self.client.post(f"{URL}{q['id']}/convert/", {}, format="json")
        self.assertEqual(self.client.post(f"{URL}{q['id']}/convert/", {}, format="json").status_code, 400)
        body = {"customer": self.customer.id, "items": [{"item": self.soda.id, "quantity": 1}]}
        self.assertEqual(self.client.put(f"{URL}{q['id']}/", body, format="json").status_code, 400)
        self.assertEqual(self.client.delete(f"{URL}{q['id']}/").status_code, 400)

    def test_status_flow_and_rejected_cannot_convert(self):
        q = self._create().data
        self.assertEqual(self.client.post(f"{URL}{q['id']}/set_status/", {"status": "sent"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"{URL}{q['id']}/set_status/", {"status": "rejected"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"{URL}{q['id']}/convert/", {}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"{URL}{q['id']}/set_status/", {"status": "converted"}, format="json").status_code, 400)

    def test_edit_replaces_lines(self):
        q = self._create().data
        body = {"customer": self.customer.id, "items": [{"item": self.soda.id, "quantity": 3}]}
        res = self.client.put(f"{URL}{q['id']}/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["items"]), 1)
        self.assertEqual(res.data["total_amount"], "15.00")

    def test_quotation_is_company_scoped(self):
        q = self._create().data
        outsider = User.objects.create_user(
            username="o.sales", email="o@x.test", role="sales",
            company=Company.objects.create(name="Other", slug="other"), password="pass",
        )
        self.client.force_authenticate(user=outsider)
        self.assertEqual(self.client.get(f"{URL}{q['id']}/").status_code, 404)
        self.assertEqual(self.client.get(URL).data, [])

    def test_printable_document(self):
        q = self._create().data
        res = self.client.get(f"/api/documents/quotation/{q['id']}/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(q["quote_number"], res.content.decode())

    def test_valid_until_string_is_stored_as_a_date(self):
        res = self._create(valid_until="2030-01-31")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data["valid_until"], "2030-01-31")
        self.assertFalse(res.data["is_expired"])
        past = self._create(valid_until="2020-01-01")
        self.assertTrue(past.data["is_expired"])
        self.assertEqual(self._create(valid_until="31/01/2030").status_code, 400)
