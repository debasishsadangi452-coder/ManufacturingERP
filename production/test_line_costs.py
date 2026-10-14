"""Per-line production cost rates, the running cost summary and the dashboard trend."""
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item, Warehouse
from production.costing import order_cost
from production.models import ProductionLine, ProductionOrder, Recipe
from sales.models import Customer, Invoice


class LineCostTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Cost Co", slug="cost-co")
        self.user = User.objects.create_user(username="admin.cost", password="x", role="admin", company=self.company)
        self.client.force_authenticate(self.user)
        self.warehouse = Warehouse.objects.create(company=self.company, name="Plant", location="HQ")
        self.product = Item.objects.create(company=self.company, name="Gadget", category="finished_good", unit="ea",
                                           selling_price=Decimal("20.00"))
        self.recipe = Recipe.objects.create(product=self.product, batch_size=1)
        self.line = ProductionLine.objects.create(company=self.company, name="Line 1")

    def make_order(self, produced=100, line=None):
        return ProductionOrder.objects.create(
            recipe=self.recipe, quantity=100, warehouse=self.warehouse, line=line, status="completed",
            produced_quantity=produced, end_time=timezone.now(),
        )

    def test_line_rates_are_added_to_order_cost(self):
        order = self.make_order(line=self.line)
        before = order_cost(order)["actual"]["total_cost"]
        res = self.client.patch(f"/api/production/lines/{self.line.id}/", {
            "machinery_cost_per_unit": "0.50", "labour_cost_per_unit": "0.25", "overhead_cost_per_unit": "0.10",
        }, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        actual = order_cost(ProductionOrder.objects.get(pk=order.pk))["actual"]
        self.assertAlmostEqual(actual["total_cost"] - before, 85.0, places=2)   # 100 units x 0.85
        self.assertAlmostEqual(actual["machine_cost"], 50.0, places=2)
        self.assertAlmostEqual(actual["labour_cost"], 25.0, places=2)

    def test_running_cost_summary_per_line_and_total(self):
        ProductionLine.objects.filter(pk=self.line.pk).update(machinery_cost_per_unit=2, labour_cost_per_unit=1)
        self.make_order(produced=10, line=self.line)
        self.make_order(produced=30, line=self.line)
        res = self.client.get("/api/production/lines/cost_summary/")
        self.assertEqual(res.status_code, 200, res.data)
        row = next(r for r in res.data["lines"] if r["line_id"] == self.line.id)
        self.assertEqual(row["units"], 40)
        self.assertEqual(row["orders"], 2)
        self.assertAlmostEqual(row["machinery_cost"], 80.0, places=2)
        self.assertAlmostEqual(row["labour_cost"], 40.0, places=2)
        self.assertAlmostEqual(res.data["totals"]["total_cost"], row["total_cost"], places=2)
        self.assertAlmostEqual(row["unit_cost"], 3.0, places=4)

    def test_trend_pairs_sales_with_production_cost(self):
        ProductionLine.objects.filter(pk=self.line.pk).update(machinery_cost_per_unit=1)
        self.make_order(produced=100, line=self.line)
        customer = Customer.objects.create(company=self.company, name="Buyer")
        Invoice.objects.create(company=self.company, customer=customer, total_amount=Decimal("500.00"))
        res = self.client.get("/api/production/cost-trend/", {"months": 3})
        self.assertEqual(res.status_code, 200, res.data)
        this_month = res.data[-1]
        self.assertEqual(len(res.data), 3)
        self.assertEqual(this_month["sales"], 500.0)
        self.assertAlmostEqual(this_month["production_cost"], 100.0, places=2)
