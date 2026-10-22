"""Sales-order profit, closing a part-delivered order, and an item's movement history."""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item, Stock, StockMovement, Warehouse
from sales.models import Customer, SalesOrder, SalesOrderItem


class ProfitCloseHistoryTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Profit Co", slug="profitco")
        self.admin = User.objects.create_user(username="a@profitco", role="admin", company=self.company, password="p")
        self.sales = User.objects.create_user(username="s@profitco", role="sales", company=self.company, password="p")
        customer = Customer.objects.create(company=self.company, name="Shop")
        self.item = Item.objects.create(company=self.company, name="Soda", category="finished_good",
                                        selling_price=Decimal("5"), purchase_cost=Decimal("2"))
        self.order = SalesOrder.objects.create(customer=customer, status="confirmed", subtotal=Decimal("50"),
                                               total_amount=Decimal("50"))
        self.line = SalesOrderItem.objects.create(sales_order=self.order, item=self.item, quantity=10, unit_price=Decimal("5"))

    def test_profit_uses_standard_cost_when_not_produced(self):
        self.client.force_authenticate(self.admin)
        res = self.client.get(f"/api/sales/sales-orders/{self.order.id}/profit/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["revenue"], res.data["cost"], res.data["profit"]), (50.0, 20.0, 30.0))
        summary = self.client.get("/api/sales/sales-orders/profit_summary/")
        self.assertEqual(summary.data["totals"]["profit"], 30.0)

    def test_profit_is_admin_only(self):
        self.client.force_authenticate(self.sales)
        self.assertEqual(self.client.get(f"/api/sales/sales-orders/{self.order.id}/profit/").status_code, 403)

    def test_close_order_needs_partial_delivery_then_drops_balance(self):
        self.client.force_authenticate(self.sales)
        url = f"/api/sales/sales-orders/{self.order.id}/close_order/"
        self.assertEqual(self.client.post(url).status_code, 400)  # nothing delivered yet
        self.line.shipped_quantity = 4
        self.line.save()
        self.assertEqual(self.client.post(url).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "delivered")
        self.assertEqual(self.client.post(url).status_code, 400)  # already closed
        self.client.force_authenticate(self.admin)
        res = self.client.get(f"/api/sales/sales-orders/{self.order.id}/profit/")
        self.assertEqual((res.data["revenue"], res.data["cost"]), (20.0, 8.0))  # only the 4 delivered

    def test_item_history_has_running_balance(self):
        wh = Warehouse.objects.create(company=self.company, name="Main", location="x")
        Stock.objects.create(item=self.item, warehouse=wh, quantity=70)
        StockMovement.objects.create(item=self.item, warehouse=wh, movement_type="IN", quantity=100, reference="PO#1 receipt")
        StockMovement.objects.create(item=self.item, warehouse=wh, movement_type="OUT", quantity=30, reference="Reserved for Production #5")
        self.client.force_authenticate(self.admin)
        res = self.client.get(f"/api/inventory/items/{self.item.id}/history/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["on_hand"], 70)
        newest, oldest = res.data["movements"]
        self.assertEqual((newest["kind"], newest["balance_after"]), ("Issued to production", 70))
        self.assertEqual((oldest["kind"], oldest["balance_after"]), ("Received from procurement", 100))
        self.assertEqual(res.data["totals"]["issued_to_production"], 30)
