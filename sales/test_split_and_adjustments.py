from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item, Stock, Warehouse
from sales.models import Invoice, SalesOrder


class SplitDeliveryAndAdjustmentTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Split Plant", slug="split-plant")
        self.user = User.objects.create_user(username="admin.split", password="pass123", role="admin", company=self.company)
        self.client.force_authenticate(self.user)
        self.warehouse = Warehouse.objects.create(company=self.company, name="Main", location="Plant")
        from sales.models import Customer
        self.customer = Customer.objects.create(company=self.company, name="Buyer A")
        self.fg = Item.objects.create(company=self.company, name="Widget", category="finished_good",
                                      unit="ea", selling_price=Decimal("10.00"))
        Stock.objects.create(item=self.fg, warehouse=self.warehouse, quantity=100)

    def _order(self, **extra):
        res = self.client.post("/api/sales/sales-orders/", {
            "customer": self.customer.id, "status": "confirmed",
            "items": [{"item": self.fg.id, "quantity": 100, "unit_price": "10.00"}], **extra,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        order = SalesOrder.objects.get(pk=res.data["id"])
        order.status = "confirmed"
        order.save(update_fields=["status"])
        return order

    def test_discounts_and_tax_set_order_total(self):
        order = self._order(discounts=[{"label": "Volume", "percent": 10}, {"label": "Loyalty", "percent": 5}],
                            tax_amount="50.00", tax_label="GST")
        # 1000 - 15% + 50
        self.assertEqual(order.subtotal, Decimal("1000.00"))
        self.assertEqual(order.discount_amount, Decimal("150.00"))
        self.assertEqual(order.total_amount, Decimal("900.00"))
        self.assertEqual(order.discounts.count(), 2)

    def test_order_without_adjustments_is_unchanged(self):
        order = self._order()
        self.assertEqual(order.total_amount, Decimal("1000.00"))
        self.assertEqual(order.discount_amount, 0)

    def test_split_deliveries_invoice_pro_rata_and_summarise(self):
        order = self._order(discounts=[{"label": "Volume", "percent": 10}], tax_amount="90.00")
        line = order.salesorderitem_set.first()
        invoices = []
        for qty in (40, 60):
            res = self.client.post(f"/api/sales/sales-orders/{order.id}/split_delivery/",
                                   {"lines": [{"order_item_id": line.id, "quantity": qty}], "invoice": "split"},
                                   format="json")
            self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
            self.assertEqual(res.data["status"], "dispatched")
            invoices.append(Invoice.objects.get(pk=res.data["invoice"]))
        self.assertEqual(Stock.objects.get(item=self.fg).quantity, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, "delivered")
        self.assertEqual(order.dispatches.count(), 2)
        # 40 units: 400 - 40 discount + 36 tax; 60 units: 600 - 60 + 54. Tax adds back to 90.
        self.assertEqual(invoices[0].total_amount, Decimal("396.00"))
        self.assertEqual(invoices[1].total_amount, Decimal("594.00"))
        self.assertEqual(sum(i.tax_amount for i in invoices), Decimal("90.00"))
        self.assertEqual(sum(i.total_amount for i in invoices), order.total_amount)

        res = self.client.post("/api/sales/summary-invoices/", {"invoice_ids": [i.id for i in invoices]}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(Decimal(str(res.data["total_amount"])), Decimal("990.00"))
        again = self.client.post("/api/sales/summary-invoices/", {"invoice_ids": [i.id for i in invoices]}, format="json")
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)

        # Every document of the order renders, and the register lists them.
        dispatch = order.dispatches.first()
        for doc_type, pk in [("dispatch_note", dispatch.id), ("delivery_note", dispatch.id),
                             ("packing_note", dispatch.id), ("invoice", invoices[0].id),
                             ("summary_invoice", res.data["id"])]:
            doc = self.client.get(f"/api/documents/{doc_type}/{pk}/?format=json")
            self.assertEqual(doc.status_code, 200, f"{doc_type}: {doc.data}")
            html = self.client.get(f"/api/documents/{doc_type}/{pk}/")
            self.assertEqual(html.status_code, 200, doc_type)
        inv_totals = dict(self.client.get(f"/api/documents/invoice/{invoices[0].id}/?format=json").data["totals"])
        self.assertEqual(inv_totals["Discount"], "-40.00")
        register = {d["type"] for sec in self.client.get("/api/documents/register/").data for d in sec["documents"]}
        self.assertTrue({"invoice", "summary_invoice", "dispatch_note", "packing_note", "delivery_note"} <= register)

    def test_split_delivery_without_invoice_then_total_invoice(self):
        order = self._order()
        line = order.salesorderitem_set.first()
        for qty in (30, 70):
            res = self.client.post(f"/api/sales/sales-orders/{order.id}/split_delivery/",
                                   {"lines": [{"order_item_id": line.id, "quantity": qty}], "invoice": "later"},
                                   format="json")
            self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
            self.assertIsNone(res.data["invoice"])
        res = self.client.post(f"/api/sales/sales-orders/{order.id}/generate_invoice/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(Decimal(str(res.data["total_amount"])), Decimal("1000.00"))

    def test_cannot_deliver_more_than_stock(self):
        order = self._order()
        line = order.salesorderitem_set.first()
        Stock.objects.filter(item=self.fg).update(quantity=20)
        res = self.client.post(f"/api/sales/sales-orders/{order.id}/split_delivery/",
                               {"lines": [{"order_item_id": line.id, "quantity": 50}]}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(order.dispatches.count(), 0)

    def test_split_production_plan_cannot_exceed_order(self):
        order = self._order()
        res = self.client.post(f"/api/sales/sales-orders/{order.id}/split_production_plan/",
                               {"item_id": self.fg.id, "parts": [{"quantity": 60}, {"quantity": 40}]}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(len(res.data["plans"]), 2)
        more = self.client.post(f"/api/sales/sales-orders/{order.id}/split_production_plan/",
                                {"item_id": self.fg.id, "parts": [{"quantity": 1}]}, format="json")
        self.assertEqual(more.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_and_json_documents(self):
        order = self._order(discounts=[{"label": "Volume", "percent": 10}])
        res = self.client.get("/api/documents/register/")
        self.assertEqual(res.status_code, 200)
        types = {d["type"] for s in res.data for d in s["documents"]}
        self.assertIn("customer_order_confirmation", types)
        for doc_type in ("customer_order_confirmation", "pro_forma_invoice", "sales_quotation"):
            doc = self.client.get(f"/api/documents/{doc_type}/{order.id}/?format=json")
            self.assertEqual(doc.status_code, 200, doc_type)
        totals = dict(self.client.get(f"/api/documents/customer_order_confirmation/{order.id}/?format=json").data["totals"])
        self.assertIn("Subtotal", totals)
