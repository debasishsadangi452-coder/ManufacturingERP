"""Multi-level BOM planning: intermediate shortage warnings and default lines."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import Item, Stock, Warehouse
from production.models import ProductionLine, ProductionOrder, ProductionPlan, Recipe, RecipeIngredient
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
        # Only the intermediate is mentioned, not the flour needed to bake it.
        self.assertNotIn("Flour", warning)

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

    def test_creating_an_order_requests_only_short_raw_materials(self):
        from inventory.models import InventoryRequest

        customer = Customer.objects.create(company=self.company, name="MTO Customer")

        # Packing 100 bags: cookies short (intermediate). Baking 1,000 cookies: flour short (raw).
        pack_order = SalesOrder.objects.create(customer=customer, status="confirmed")
        pack_item = SalesOrderItem.objects.create(sales_order=pack_order, item=self.bag, quantity=100)
        pack_plan = ProductionPlan.objects.create(
            company=self.company, sales_order=pack_order, sales_order_item=pack_item,
            customer=customer, item=self.bag, order_quantity=100,
            planned_quantity=100, status="approved",
        )
        pack_response = self.client.post(f"/api/production/production-plans/{pack_plan.id}/convert/")
        self.assertEqual(pack_response.status_code, 201, pack_response.data)
        self.assertFalse(
            InventoryRequest.objects.filter(
                production_order_id=pack_response.data["production_order_id"]
            ).exists()
        )

        # Stock in another warehouse still counts.
        other = Warehouse.objects.create(company=self.company, name="Annex", location="Back")
        bake_order = SalesOrder.objects.create(customer=customer, status="confirmed")
        bake_item = SalesOrderItem.objects.create(sales_order=bake_order, item=self.cookie, quantity=1000)
        bake_plan = ProductionPlan.objects.create(
            company=self.company, sales_order=bake_order, sales_order_item=bake_item,
            customer=customer, item=self.cookie, order_quantity=1000,
            planned_quantity=1000, status="approved",
        )
        bake_response = self.client.post(
            f"/api/production/production-plans/{bake_plan.id}/convert/",
            {"warehouse": other.id},
            format="json",
        )
        self.assertEqual(bake_response.status_code, 201, bake_response.data)
        bake_id = bake_response.data["production_order_id"]
        [request] = InventoryRequest.objects.filter(production_order_id=bake_id)
        self.assertEqual((request.item, request.quantity), (self.flour, 7))  # 12 kg needed, 5 on hand
        self.assertEqual(request.status, "pending")
        request_data = self.client.get("/api/inventory/requests/").data
        serialized_request = next(row for row in request_data if row["id"] == request.id)
        self.assertEqual(serialized_request["production_order"], bake_id)
        self.assertEqual(serialized_request["production_plan_id"], bake_plan.id)
        self.assertEqual(serialized_request["production_plan_number"], bake_plan.plan_number)
        self.assertEqual(serialized_request["item_unit"], "kg")
        self.assertEqual(serialized_request["status"], "pending")
        self.assertIsNone(serialized_request["purchase_order_status"])
