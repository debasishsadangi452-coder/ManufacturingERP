from django.test import TestCase
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient
from rest_framework import status

from accounts.models import Company, User
from inventory.models import Item, Warehouse, Stock
from sales.models import Customer, SalesOrder, SalesOrderItem
from production.models import Recipe, RecipeIngredient, ProductionLine, ProductionOrder, ProductionPlan


class ProductionPlanWorkflowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name="BrewCorp Inc")

        # Users
        self.admin_user = User.objects.create_user(
            username="admin_user", password="password123", role="admin", company=self.company
        )
        self.production_user = User.objects.create_user(
            username="prod_user", password="password123", role="production", company=self.company
        )
        self.sales_user = User.objects.create_user(
            username="sales_user", password="password123", role="sales", company=self.company
        )
        self.unauth_user = User.objects.create_user(
            username="viewer_user", password="password123", role="hr", company=self.company
        )

        # Inventory & Warehouse
        self.warehouse = Warehouse.objects.create(
            name="Main Facility", location="Zone A", company=self.company
        )
        self.finished_item = Item.objects.create(
            name="Stout Craft Beer",
            category="finished_good",
            unit="bottle",
            selling_price="15.00",
            company=self.company,
        )
        self.raw_item = Item.objects.create(
            name="Malted Barley",
            category="raw_material",
            unit="kg",
            selling_price="2.00",
            company=self.company,
        )
        Stock.objects.create(
            item=self.raw_item,
            warehouse=self.warehouse,
            quantity=1000.0,
        )

        # Recipe
        self.recipe = Recipe.objects.create(
            product=self.finished_item,
            batch_size=100.0,
        )
        RecipeIngredient.objects.create(
            recipe=self.recipe,
            item=self.raw_item,
            quantity=50.0,
        )

        # Production Line
        self.line = ProductionLine.objects.create(
            name="Line 1",
            location="Building 1",
            capacity=200.0,
            company=self.company,
        )

        # Customer & Sales Order
        self.customer = Customer.objects.create(
            name="Pub Chain Ltd", company=self.company
        )
        self.sales_order = SalesOrder.objects.create(
            customer=self.customer,
            status="pending",
            total_amount=1500.0,
        )
        self.order_item = SalesOrderItem.objects.create(
            sales_order=self.sales_order,
            item=self.finished_item,
            quantity=100.0,
        )

    def test_production_plan_model_creation(self):
        """A. Production Plan model creation & auto plan_number"""
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            sales_order_item=self.order_item,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            notes="Initial MTO Plan",
        )
        self.assertTrue(plan.plan_number.startswith(f"PPO-{self.sales_order.id}-"))
        self.assertEqual(plan.status, "draft")
        self.assertEqual(plan.planned_quantity, 100.0)
        self.assertFalse(plan.is_converted)

    def test_production_plan_quantity_validation(self):
        """B. Required-field & negative/zero quantity validation in serializer"""
        self.client.force_authenticate(user=self.production_user)
        payload = {
            "sales_order": self.sales_order.id,
            "sales_order_item": self.order_item.id,
            "item": self.finished_item.id,
            "order_quantity": 100.0,
            "planned_quantity": -10.0,
        }
        res = self.client.post("/api/production/production-plans/", payload)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("planned_quantity", res.data)

    def test_customer_order_to_production_plan_creation(self):
        """C. Customer Order -> Production Plan creation via sales action"""
        self.sales_order.status = "confirmed"
        self.sales_order.save(update_fields=["status"])
        self.client.force_authenticate(user=self.sales_user)
        res = self.client.post(
            f"/api/sales/sales-orders/{self.sales_order.id}/create_production_plan/",
            {"planned_quantity": 100.0, "notes": "Planned from Sales"},
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(res.data["plans"]), 1)
        created_plan_data = res.data["plans"][0]
        self.assertEqual(created_plan_data["sales_order"], self.sales_order.id)
        self.assertEqual(created_plan_data["item"], self.finished_item.id)
        self.assertEqual(created_plan_data["planned_quantity"], 100.0)
        self.assertEqual(created_plan_data["status"], "planned")

        plan = ProductionPlan.objects.get(id=created_plan_data["id"])
        self.assertEqual(plan.customer, self.customer)
        self.assertEqual(plan.company, self.company)

    def test_invalid_customer_order_rejection(self):
        """D. Cancelled / Delivered Customer Order rejection"""
        self.sales_order.status = "cancelled"
        self.sales_order.save()

        self.client.force_authenticate(user=self.sales_user)
        res = self.client.post(
            f"/api/sales/sales-orders/{self.sales_order.id}/create_production_plan/",
            {"planned_quantity": 100.0},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot create production plan", res.data["error"])

    def test_duplicate_plan_creation_prevention(self):
        """Prevent duplicate active plan creation for same order line"""
        self.sales_order.status = "confirmed"
        self.sales_order.save(update_fields=["status"])
        ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            sales_order_item=self.order_item,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            status="planned",
        )

        self.client.force_authenticate(user=self.sales_user)
        res = self.client.post(
            f"/api/sales/sales-orders/{self.sales_order.id}/create_production_plan/",
            {"item_id": self.finished_item.id, "planned_quantity": 100.0},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", res.data["error"])

    def test_production_plan_retrieval(self):
        """F. Production Plan list & detail retrieval"""
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            sales_order_item=self.order_item,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            status="planned",
        )

        self.client.force_authenticate(user=self.production_user)
        list_res = self.client.get("/api/production/production-plans/")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        # Should include this plan
        results = list_res.data if isinstance(list_res.data, list) else list_res.data.get("results", [])
        plan_ids = [p["id"] for p in results]
        self.assertIn(plan.id, plan_ids)

        detail_res = self.client.get(f"/api/production/production-plans/{plan.id}/")
        self.assertEqual(detail_res.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_res.data["plan_number"], plan.plan_number)
        self.assertEqual(detail_res.data["sales_order_number"], f"SO-{self.sales_order.id}")
        self.assertEqual(detail_res.data["customer_name"], self.customer.name)

    def test_production_plan_status_transition(self):
        """G. Production Plan status change endpoint"""
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            status="draft",
        )

        self.client.force_authenticate(user=self.production_user)
        res = self.client.post(
            f"/api/production/production-plans/{plan.id}/change_status/",
            {"status": "planned"},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "planned")

    def test_convert_to_production_order_success_and_traceability(self):
        """H, I, J. Production Plan -> Production Order conversion & complete traceability"""
        self.sales_order.status = "confirmed"
        self.sales_order.save(update_fields=["status"])
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            sales_order_item=self.order_item,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            status="planned",
        )

        self.client.force_authenticate(user=self.production_user)
        res = self.client.post(
            f"/api/production/production-plans/{plan.id}/convert/",
            {"warehouse": self.warehouse.id, "line": self.line.id},
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        prod_order_id = res.data["production_order_id"]

        plan.refresh_from_db()
        self.assertEqual(plan.status, "converted")
        self.assertTrue(plan.is_converted)

        prod_order = ProductionOrder.objects.get(id=prod_order_id)
        # Traceability assertions
        self.assertEqual(prod_order.production_plan, plan)
        self.assertEqual(prod_order.sales_order, self.sales_order)
        self.assertEqual(prod_order.sales_order.customer, self.customer)
        self.assertEqual(prod_order.recipe.product, self.finished_item)
        self.assertEqual(prod_order.quantity, 100.0)
        self.assertEqual(prod_order.warehouse, self.warehouse)
        self.assertEqual(prod_order.line, self.line)

        # Serializer includes traceability fields
        order_detail_res = self.client.get(f"/api/production/production-orders/{prod_order.id}/")
        self.assertEqual(order_detail_res.status_code, status.HTTP_200_OK)
        self.assertEqual(order_detail_res.data["production_plan"], plan.id)
        self.assertEqual(order_detail_res.data["production_plan_number"], plan.plan_number)
        self.assertEqual(order_detail_res.data["sales_order_number"], f"SO-{self.sales_order.id}")
        self.assertEqual(order_detail_res.data["customer_name"], self.customer.name)

    def test_prevent_duplicate_conversion(self):
        """K. Prevent duplicate conversion"""
        self.sales_order.status = "confirmed"
        self.sales_order.save(update_fields=["status"])
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            sales_order_item=self.order_item,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            status="planned",
        )

        self.client.force_authenticate(user=self.production_user)
        # First conversion succeeds
        res1 = self.client.post(f"/api/production/production-plans/{plan.id}/convert/")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Second conversion must be rejected
        res2 = self.client.post(f"/api/production/production-plans/{plan.id}/convert/")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been converted", res2.data["error"])

        # Directly invoking model method also raises ValidationError
        with self.assertRaises(ValidationError):
            plan.convert_to_production_order()

    def test_conversion_atomic_rollback_on_failure(self):
        """L. Transaction rollback when recipe is missing"""
        unsupported_item = Item.objects.create(
            name="Raw Malt", category="finished_good", unit="kg", company=self.company
        )
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            customer=self.customer,
            item=unsupported_item,
            order_quantity=50.0,
            planned_quantity=50.0,
            status="planned",
        )

        self.client.force_authenticate(user=self.production_user)
        res = self.client.post(f"/api/production/production-plans/{plan.id}/convert/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        plan.refresh_from_db()
        # Status must NOT have been changed to converted
        self.assertEqual(plan.status, "planned")
        self.assertFalse(plan.is_converted)
        self.assertEqual(ProductionOrder.objects.filter(production_plan=plan).count(), 0)

    def test_permissions(self):
        """M. Unauthorized role cannot convert or access production plan"""
        plan = ProductionPlan.objects.create(
            company=self.company,
            sales_order=self.sales_order,
            customer=self.customer,
            item=self.finished_item,
            order_quantity=100.0,
            planned_quantity=100.0,
            status="planned",
        )

        # Anonymous cannot access
        anon_client = APIClient()
        res = anon_client.get("/api/production/production-plans/")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        # Sales user can read plans but cannot convert them (only IsProduction | IsAdmin)
        self.client.force_authenticate(user=self.sales_user)
        res = self.client.get(f"/api/production/production-plans/{plan.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        convert_res = self.client.post(f"/api/production/production-plans/{plan.id}/convert/")
        self.assertEqual(convert_res.status_code, status.HTTP_403_FORBIDDEN)

    def test_mark_ready_for_production_backward_compatibility(self):
        """N. mark_ready_for_production creates/links ProductionPlan automatically"""
        self.client.force_authenticate(user=self.sales_user)
        res = self.client.post(f"/api/sales/sales-orders/{self.sales_order.id}/mark_ready_for_production/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Verify a ProductionPlan was created and linked
        plans = ProductionPlan.objects.filter(sales_order=self.sales_order)
        self.assertEqual(plans.count(), 1)
        plan = plans.first()
        self.assertEqual(plan.status, "converted")
        self.assertEqual(plan.item, self.finished_item)

        # Verify ProductionOrder references this plan
        prod_order = ProductionOrder.objects.filter(sales_order=self.sales_order).first()
        self.assertIsNotNone(prod_order)
        self.assertEqual(prod_order.production_plan, plan)


class Phase2MRPTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name="Apex Manufacturing")

        # Users
        self.admin_user = User.objects.create_user(
            username="mrp_admin", password="password123", role="admin", company=self.company
        )
        self.prod_user = User.objects.create_user(
            username="mrp_planner", password="password123", role="production", company=self.company
        )
        self.viewer_user = User.objects.create_user(
            username="mrp_viewer", password="password123", role="hr", company=self.company
        )

        # UOMs
        from inventory.models import UnitOfMeasure, BOM, BOMLine
        self.uom_kg = UnitOfMeasure.objects.get_or_create(code="kg", defaults={"name": "Kilogram", "dimension": "mass", "to_base_factor": "1000", "is_base": False})[0]
        self.uom_g = UnitOfMeasure.objects.get_or_create(code="g", defaults={"name": "Gram", "dimension": "mass", "to_base_factor": "1", "is_base": True})[0]
        self.uom_each = UnitOfMeasure.objects.get_or_create(code="each", defaults={"name": "Each", "dimension": "count", "to_base_factor": "1", "is_base": True})[0]

        # Warehouse
        self.warehouse = Warehouse.objects.create(name="Central Plant Wh", location="Plant 1", company=self.company)

        # Customer & Sales Order
        self.customer = Customer.objects.create(name="Globex Corp", company=self.company)
        self.sales_order = SalesOrder.objects.create(customer=self.customer, total_amount=1500.0)

    def test_item_types_raw_material_semi_finished_finished_good(self):
        """A, B, C: Item model supports raw_material, semi_finished, and finished_good categories"""
        rm = Item.objects.create(name="Raw Material A", category="raw_material", company=self.company)
        sf = Item.objects.create(name="Semi Finished X", category="semi_finished", company=self.company)
        fg = Item.objects.create(name="Finished Good Z", category="finished_good", company=self.company)

        self.assertTrue(rm.is_raw_material)
        self.assertFalse(rm.is_semi_finished)
        self.assertFalse(rm.is_finished_good)

        self.assertFalse(sf.is_raw_material)
        self.assertTrue(sf.is_semi_finished)
        self.assertFalse(sf.is_finished_good)

        self.assertFalse(fg.is_raw_material)
        self.assertFalse(fg.is_semi_finished)
        self.assertTrue(fg.is_finished_good)

    def test_bom_creation_and_component_validation(self):
        """D, E: BOM creation allows finished goods and semi-finished parents, with valid components"""
        from inventory.models import BOM, BOMLine
        fg = Item.objects.create(name="FG Item", category="finished_good", company=self.company)
        sf = Item.objects.create(name="SF Item", category="semi_finished", company=self.company)
        rm = Item.objects.create(name="RM Item", category="raw_material", company=self.company)

        # Finished good BOM
        bom_fg = BOM.objects.create(finished_good=fg, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom_fg, raw_material=sf, quantity=2.0, unit="unit")
        BOMLine.objects.create(bom=bom_fg, raw_material=rm, quantity=5.0, unit="unit")
        self.assertEqual(bom_fg.lines.count(), 2)

        # Semi-finished BOM
        bom_sf = BOM.objects.create(finished_good=sf, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom_sf, raw_material=rm, quantity=10.0, unit="unit")
        self.assertEqual(bom_sf.lines.count(), 1)

    def test_zero_and_negative_quantity_rejection(self):
        """F, G: Zero and negative quantities are rejected in BOMLine"""
        from inventory.models import BOM, BOMLine
        fg = Item.objects.create(name="FG Qty Test", category="finished_good", company=self.company)
        rm = Item.objects.create(name="RM Qty Test", category="raw_material", company=self.company)
        bom = BOM.objects.create(finished_good=fg)

        with self.assertRaises(ValidationError):
            line = BOMLine(bom=bom, raw_material=rm, quantity=0.0)
            line.clean()

        with self.assertRaises(ValidationError):
            line = BOMLine(bom=bom, raw_material=rm, quantity=-5.0)
            line.clean()

    def test_circular_bom_rejection(self):
        """H: Circular BOM structures are rejected (A -> B -> C -> A)"""
        from inventory.models import BOM, BOMLine
        item_a = Item.objects.create(name="Item A", category="semi_finished", company=self.company)
        item_b = Item.objects.create(name="Item B", category="semi_finished", company=self.company)
        item_c = Item.objects.create(name="Item C", category="semi_finished", company=self.company)

        # A -> B
        bom_a = BOM.objects.create(finished_good=item_a)
        BOMLine.objects.create(bom=bom_a, raw_material=item_b, quantity=1.0)

        # B -> C
        bom_b = BOM.objects.create(finished_good=item_b)
        BOMLine.objects.create(bom=bom_b, raw_material=item_c, quantity=1.0)

        # Now try to add A to C's BOM: C -> A
        bom_c = BOM.objects.create(finished_good=item_c)
        with self.assertRaises(ValidationError):
            line = BOMLine(bom=bom_c, raw_material=item_a, quantity=1.0)
            line.clean()

    def test_missing_bom_handling(self):
        """I: Missing BOM returns clear error instead of zero requirements"""
        from production.mrp import calculate_mrp_for_plan
        fg = Item.objects.create(name="FG Without BOM", category="finished_good", company=self.company)
        plan = ProductionPlan.objects.create(
            sales_order=self.sales_order,
            customer=self.customer,
            item=fg,
            order_quantity=50.0,
            planned_quantity=50.0,
            company=self.company,
        )

        with self.assertRaises(ValidationError) as ctx:
            calculate_mrp_for_plan(plan)
        self.assertIn("Active BOM not found", str(ctx.exception))

        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(f"/api/production/production-plans/{plan.id}/mrp/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Active BOM not found", str(response.data["error"]))

    def test_single_level_mrp_calculation(self):
        """L: Single-level MRP calculation with gross, available, net requirement and shortage"""
        from inventory.models import BOM, BOMLine
        from production.mrp import calculate_mrp_for_plan
        fg = Item.objects.create(name="Simple Cookie", category="finished_good", company=self.company)
        rm_flour = Item.objects.create(name="Flour", category="raw_material", unit="kg", company=self.company)
        rm_sugar = Item.objects.create(name="Sugar", category="raw_material", unit="kg", company=self.company)

        bom = BOM.objects.create(finished_good=fg, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom, raw_material=rm_flour, quantity=0.5, unit="kg")
        BOMLine.objects.create(bom=bom, raw_material=rm_sugar, quantity=0.2, unit="kg")

        Stock.objects.create(item=rm_flour, warehouse=self.warehouse, quantity=40.0)
        Stock.objects.create(item=rm_sugar, warehouse=self.warehouse, quantity=10.0)

        plan = ProductionPlan.objects.create(
            sales_order=self.sales_order,
            customer=self.customer,
            item=fg,
            order_quantity=100.0,
            planned_quantity=100.0,
            company=self.company,
        )

        result = calculate_mrp_for_plan(plan, warehouse=self.warehouse)
        self.assertTrue(result["has_shortage"])
        self.assertEqual(len(result["raw_materials"]), 2)

        flour_res = next(r for r in result["raw_materials"] if r["item_name"] == "Flour")
        self.assertEqual(flour_res["required_quantity"], 50.0)
        self.assertEqual(flour_res["available_quantity"], 40.0)
        self.assertEqual(flour_res["shortage_quantity"], 10.0)
        self.assertEqual(flour_res["status"], "shortage")

    def test_mrp_uses_existing_formula_when_bom_is_not_configured(self):
        from production.models import Recipe, RecipeIngredient
        from production.mrp import calculate_mrp_for_plan

        fg = Item.objects.create(name="Formula-backed Product", category="finished_good", company=self.company)
        flour = Item.objects.create(name="Formula Flour", category="raw_material", unit="kg", company=self.company)
        recipe = Recipe.objects.create(product=fg, batch_size=10)
        RecipeIngredient.objects.create(recipe=recipe, item=flour, quantity=4)
        plan = ProductionPlan.objects.create(
            sales_order=self.sales_order,
            customer=self.customer,
            item=fg,
            order_quantity=25,
            planned_quantity=25,
            company=self.company,
        )

        result = calculate_mrp_for_plan(plan, warehouse=self.warehouse)

        self.assertEqual(result["source"], "formula")
        self.assertEqual(result["formula"]["id"], recipe.id)
        self.assertIsNone(result["bom"])
        self.assertEqual(result["raw_materials"][0]["required_quantity"], 12)

    def test_uom_conversion_in_mrp(self):
        """P: UOM conversion handles kg to g properly"""
        from inventory.models import BOM, BOMLine
        from production.mrp import calculate_mrp_for_plan
        fg = Item.objects.create(name="Special Blend", category="finished_good", company=self.company)
        rm_spice = Item.objects.create(name="Spice", category="raw_material", base_unit=self.uom_g, unit="g", company=self.company)

        bom = BOM.objects.create(finished_good=fg, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom, raw_material=rm_spice, quantity=2.0, unit="kg", unit_of_measure=self.uom_kg)

        Stock.objects.create(item=rm_spice, warehouse=self.warehouse, quantity=1500.0)

        plan = ProductionPlan.objects.create(
            sales_order=self.sales_order,
            customer=self.customer,
            item=fg,
            order_quantity=1.0,
            planned_quantity=1.0,
            company=self.company,
        )

        result = calculate_mrp_for_plan(plan, warehouse=self.warehouse)
        spice_res = result["raw_materials"][0]
        self.assertEqual(spice_res["required_quantity"], 2000.0)
        self.assertEqual(spice_res["available_quantity"], 1500.0)
        self.assertEqual(spice_res["shortage_quantity"], 500.0)
        self.assertEqual(spice_res["unit"], "g")

    def test_step_25_exact_end_to_end_mrp_scenario(self):
        """
        STEP 25 — EXAMPLE END-TO-END TEST
        FINISHED-Z = 100 units
        SEMI-FINISHED-X: RAW-A = 5 kg, RAW-B = 2 kg
        FINISHED-Z: SEMI-FINISHED-X = 3 kg, RAW-B = 1 kg
        Inventory: RAW-A = 1000 kg, RAW-B = 800 kg, SEMI-FINISHED-X = 100 kg
        Expected MRP:
        SEMI-FINISHED-X: Required = 300 kg, Available = 100 kg, Shortage = 200 kg
        RAW-A: Required = 1000 kg, Available = 1000 kg, Shortage = 0
        RAW-B: Required = 500 kg, Available = 800 kg, Shortage = 0
        """
        from inventory.models import BOM, BOMLine
        from production.mrp import calculate_mrp_for_plan
        raw_a = Item.objects.create(name="RAW-A", category="raw_material", unit="kg", company=self.company)
        raw_b = Item.objects.create(name="RAW-B", category="raw_material", unit="kg", company=self.company)
        sf_x = Item.objects.create(name="SEMI-FINISHED-X", category="semi_finished", unit="kg", company=self.company)
        fg_z = Item.objects.create(name="FINISHED-Z", category="finished_good", unit="units", company=self.company)

        # BOM for SEMI-FINISHED-X
        bom_sf_x = BOM.objects.create(finished_good=sf_x, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom_sf_x, raw_material=raw_a, quantity=5.0, unit="kg")
        BOMLine.objects.create(bom=bom_sf_x, raw_material=raw_b, quantity=2.0, unit="kg")

        # BOM for FINISHED-Z
        bom_fg_z = BOM.objects.create(finished_good=fg_z, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom_fg_z, raw_material=sf_x, quantity=3.0, unit="kg")
        BOMLine.objects.create(bom=bom_fg_z, raw_material=raw_b, quantity=1.0, unit="kg")

        # Inventory Setup
        Stock.objects.create(item=raw_a, warehouse=self.warehouse, quantity=1000.0)
        Stock.objects.create(item=raw_b, warehouse=self.warehouse, quantity=800.0)
        Stock.objects.create(item=sf_x, warehouse=self.warehouse, quantity=100.0)

        # Record stock snapshot before MRP run to verify MRP does NOT modify stock (V, W)
        stock_a_before = Stock.objects.get(item=raw_a, warehouse=self.warehouse).quantity
        stock_b_before = Stock.objects.get(item=raw_b, warehouse=self.warehouse).quantity
        stock_sf_before = Stock.objects.get(item=sf_x, warehouse=self.warehouse).quantity

        # Create Production Plan: FINISHED-Z = 100 units
        plan = ProductionPlan.objects.create(
            sales_order=self.sales_order,
            customer=self.customer,
            item=fg_z,
            order_quantity=100.0,
            planned_quantity=100.0,
            company=self.company,
        )

        # Run MRP Engine via service
        result = calculate_mrp_for_plan(plan, warehouse=self.warehouse)

        # 1. SEMI-FINISHED-X: Required = 300 kg, Available = 100 kg, Shortage = 200 kg
        sf_res = next(item for item in result["semi_finished"] if item["item_name"] == "SEMI-FINISHED-X")
        self.assertEqual(sf_res["required_quantity"], 300.0)
        self.assertEqual(sf_res["available_quantity"], 100.0)
        self.assertEqual(sf_res["net_requirement"], 200.0)
        self.assertEqual(sf_res["shortage_quantity"], 200.0)
        self.assertEqual(sf_res["status"], "shortage")

        # Only the 200 kg semi-finished shortage is produced; stocked
        # semi-finished goods are not exploded into additional raw demand.
        raw_a_res = next(item for item in result["raw_materials"] if item["item_name"] == "RAW-A")
        self.assertEqual(raw_a_res["required_quantity"], 1000.0)
        self.assertEqual(raw_a_res["available_quantity"], 1000.0)
        self.assertEqual(raw_a_res["net_requirement"], 0.0)
        self.assertEqual(raw_a_res["shortage_quantity"], 0.0)
        self.assertEqual(raw_a_res["status"], "available")

        # 3. RAW-B: Required = 500 kg (400 + 100), Available = 800 kg, Shortage = 0
        raw_b_res = next(item for item in result["raw_materials"] if item["item_name"] == "RAW-B")
        self.assertEqual(raw_b_res["required_quantity"], 500.0)
        self.assertEqual(raw_b_res["available_quantity"], 800.0)
        self.assertEqual(raw_b_res["net_requirement"], 0.0)
        self.assertEqual(raw_b_res["shortage_quantity"], 0.0)
        self.assertEqual(raw_b_res["status"], "available")

        # Verify MRP did not modify stock (V)
        self.assertEqual(Stock.objects.get(item=raw_a, warehouse=self.warehouse).quantity, stock_a_before)
        self.assertEqual(Stock.objects.get(item=raw_b, warehouse=self.warehouse).quantity, stock_b_before)
        self.assertEqual(Stock.objects.get(item=sf_x, warehouse=self.warehouse).quantity, stock_sf_before)

        # Verify MRP did not create any Purchase Orders (W)
        from procurement.models import PurchaseOrder
        self.assertEqual(PurchaseOrder.objects.count(), 0)

        # Test API Endpoint (X, Y)
        self.client.force_authenticate(user=self.prod_user)
        api_res = self.client.get(f"/api/production/production-plans/{plan.id}/mrp/?warehouse={self.warehouse.id}")
        self.assertEqual(api_res.status_code, status.HTTP_200_OK)
        self.assertEqual(api_res.data["plan_number"], plan.plan_number)
        self.assertTrue(api_res.data["has_shortage"])
        self.assertEqual(len(api_res.data["semi_finished"]), 1)
        self.assertEqual(len(api_res.data["raw_materials"]), 2)

        # Test permission checks: Unauthorized user cannot run MRP (X)
        self.client.force_authenticate(user=self.viewer_user)
        viewer_res = self.client.get(f"/api/production/production-plans/{plan.id}/mrp/")
        self.assertEqual(viewer_res.status_code, status.HTTP_403_FORBIDDEN)
