"""Multi-level BOM planning: intermediate shortage warnings and default lines."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import Item, Stock, Warehouse
from production.models import ProductionLine, ProductionOrder, Recipe, RecipeIngredient
from production.planning import default_line_for, material_check
from sales.models import Customer, SalesOrder, SalesOrderItem


class IntermediatePlanningTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Cookie Co", slug="cookieco")
        self.admin = User.objects.create_user(username="ada.admin@cookieco", role="admin",
                                              company=self.company, password="pass")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

        self.plant = Warehouse.objects.create(company=self.company, name="Plant", location="Floor")
        self.baking = ProductionLine.objects.create(company=self.company, name="Baking")
        self.packing = ProductionLine.objects.create(company=self.company, name="Packing")

        self.flour = Item.objects.create(company=self.company, name="Flour", category="raw_material", unit="kg")
        self.cookie = Item.objects.create(company=self.company, name="Baked Cookie", category="intermediate", unit="each")
        self.bag = Item.objects.create(company=self.company, name="Cookie Bag", category="finished_good", unit="bag")

        self.bake = Recipe.objects.create(product=self.cookie, batch_size=1000, default_line=self.baking)
        RecipeIngredient.objects.create(recipe=self.bake, item=self.flour, quantity=12)
        self.pack = Recipe.objects.create(product=self.bag, batch_size=10, default_line=self.packing)
        RecipeIngredient.objects.create(recipe=self.pack, item=self.cookie, quantity=120)

        Stock.objects.create(item=self.cookie, warehouse=self.plant, quantity=600)  # enough for 50 bags
        Stock.objects.create(item=self.flour, warehouse=self.plant, quantity=5)

    def test_material_check_warns_to_produce_the_intermediate_first(self):
        check = material_check(self.pack, 100)  # 10 batches -> 1,200 cookies, 600 on hand
        self.assertFalse(check["can_produce"])
        [warning] = check["intermediate_warnings"]
        self.assertIn("Produce 600 more Baked Cookie first: 1 batch(es) of 1,000", warning)
        # ...and that the flour to bake them is short too (12 kg needed, 5 on hand).
        self.assertIn("To make it: Short 7 kg of Flour", warning)

        ok = material_check(self.pack, 50)
        self.assertTrue(ok["can_produce"])
        self.assertEqual(ok["warnings"], [])

    def test_material_check_endpoint_and_order_warning(self):
        res = self.client.get(f"/api/production/recipes/{self.pack.id}/material_check/", {"quantity": 100})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["intermediate_warnings"]), 1)

        # Created without a line -> the recipe's predefined line; the work order carries the warning.
        res = self.client.post("/api/production/production-orders/", {
            "recipe": self.pack.id, "quantity": 100, "warehouse": self.plant.id,
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        order = ProductionOrder.objects.get(pk=res.data["id"])
        self.assertEqual(order.line, self.packing)
        listed = self.client.get(f"/api/production/production-orders/{order.id}/").data
        self.assertEqual(len(listed["intermediate_warnings"]), 1)

        # Completing it explains that the cookies must be produced first.
        order.status = "running"
        order.save()
        res = self.client.post(f"/api/production/production-orders/{order.id}/complete/")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Baked Cookie is an intermediate: produce 600 more first", res.data["error"])

    def test_an_explicit_line_is_kept_and_maintenance_lines_are_skipped(self):
        res = self.client.post("/api/production/production-orders/", {
            "recipe": self.pack.id, "quantity": 10, "warehouse": self.plant.id, "line": self.baking.id,
        }, format="json")
        self.assertEqual(ProductionOrder.objects.get(pk=res.data["id"]).line, self.baking)

        self.packing.status = "maintenance"
        self.packing.save()
        self.assertIsNone(default_line_for(self.pack))

    def test_order_planned_from_a_sales_order_goes_to_the_default_line(self):
        customer = Customer.objects.create(company=self.company, name="Corner Shop")
        so = SalesOrder.objects.create(customer=customer, status="pending")
        SalesOrderItem.objects.create(sales_order=so, item=self.bag, quantity=100)

        res = self.client.post(f"/api/sales/sales-orders/{so.id}/mark_ready_for_production/")
        self.assertEqual(res.status_code, 200, res.data)
        order = ProductionOrder.objects.get(sales_order=so)
        self.assertEqual(order.line, self.packing)

    def test_default_line_must_belong_to_the_company(self):
        other = Company.objects.create(name="Other Co", slug="otherco")
        foreign_line = ProductionLine.objects.create(company=other, name="Theirs")
        res = self.client.patch(f"/api/production/recipes/{self.pack.id}/", {"default_line": foreign_line.id},
                                format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch(f"/api/production/recipes/{self.pack.id}/", {"default_line": self.baking.id},
                                format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["default_line_name"], "Baking")
