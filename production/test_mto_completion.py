from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import BOM, BOMLine, InventoryRequest, Item, ItemUOMConversion, Stock, UnitOfMeasure, Warehouse
from inventory.uom import convert
from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor
from production.models import (
    ManufacturingSettings,
    ProductionPlan,
    ProductionOrder,
    Recipe,
    RecipeIngredient,
)
from production.mrp import calculate_mrp_for_plan
from sales.models import Customer, SalesOrder, SalesOrderItem


class MTOCompletionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="MTO Completion Co")
        self.customer = Customer.objects.create(company=self.company, name="Customer")
        self.order = SalesOrder.objects.create(customer=self.customer, status="confirmed")
        self.warehouse = Warehouse.objects.create(
            company=self.company, name="Production", location="Plant", warehouse_type="production"
        )
        self.finished = Item.objects.create(
            company=self.company, name="Finished", category="finished_good", unit="each"
        )
        self.raw = Item.objects.create(
            company=self.company, name="Material", category="raw_material", unit="kg"
        )
        self.order_item = SalesOrderItem.objects.create(
            sales_order=self.order, item=self.finished, quantity=10
        )
        self.recipe = Recipe.objects.create(product=self.finished, batch_size=1)
        RecipeIngredient.objects.create(recipe=self.recipe, item=self.raw, quantity=2)

    def test_item_specific_uom_conversion(self):
        case = UnitOfMeasure.objects.create(
            company=self.company, code="case", name="Case", dimension="count"
        )
        each = UnitOfMeasure.objects.create(
            company=self.company, code="each", name="Each", dimension="count", is_base=True
        )
        ItemUOMConversion.objects.create(item=self.raw, from_unit=case, to_unit=each, factor=Decimal("12"))

        self.assertEqual(convert(2, case, each, item=self.raw), Decimal("24"))
        self.assertEqual(convert(24, each, case, item=self.raw), Decimal("2"))

    def test_mrp_normalizes_open_purchase_quantity_to_stock_uom(self):
        gram = UnitOfMeasure.objects.create(
            company=self.company, code="g", name="Gram", dimension="mass", to_base_factor=1, is_base=True
        )
        kilogram = UnitOfMeasure.objects.create(
            company=self.company, code="kg", name="Kilogram", dimension="mass",
            to_base_factor=1000,
        )
        self.raw.base_unit = gram
        self.raw.purchase_unit = kilogram
        self.raw.save(update_fields=["base_unit", "purchase_unit"])
        bom = BOM.objects.create(finished_good=self.finished, version="1.0", is_active=True)
        BOMLine.objects.create(
            bom=bom, raw_material=self.raw, quantity=10, unit="kg", unit_of_measure=kilogram
        )
        vendor = Vendor.objects.create(company=self.company, name="Unit Vendor")
        po = PurchaseOrder.objects.create(vendor=vendor, status="ordered")
        line = PurchaseOrderItem.objects.create(purchase_order=po, item=self.raw, quantity=4)
        self.assertEqual(line.unit_of_measure, kilogram)
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=self.order, sales_order_item=self.order_item,
            customer=self.customer, item=self.finished, order_quantity=10, planned_quantity=1,
        )

        result = calculate_mrp_for_plan(plan, warehouse=self.warehouse)
        material = result["raw_materials"][0]
        self.assertEqual(material["required_quantity"], 10000)
        self.assertEqual(material["on_order_quantity"], 4000)
        self.assertEqual(material["shortage_quantity"], 6000)

    def test_mrp_automatically_creates_one_inventory_request_for_each_short_raw_material(self):
        self.finished.company = self.company
        self.finished.save(update_fields=["company"])
        self.raw.company = self.company
        self.raw.save(update_fields=["company"])
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=self.order, sales_order_item=self.order_item,
            customer=self.customer, item=self.finished, order_quantity=10, planned_quantity=10,
        )
        url = f"/api/production/production-plans/{plan.id}/mrp/?warehouse={self.warehouse.id}"
        sales_user = User.objects.create_user(
            username="mto-sales-mrp",
            password="test-password",
            role="sales",
            company=self.company,
        )
        sales_client = APIClient()
        sales_client.force_authenticate(sales_user)
        sales_response = sales_client.get(url)
        self.assertEqual(sales_response.status_code, 200, sales_response.data)
        self.assertFalse(InventoryRequest.objects.filter(production_plan=plan).exists())

        user = User.objects.create_user(
            username="mto-production-mrp",
            password="test-password",
            role="production",
            company=self.company,
        )
        client = APIClient()
        client.force_authenticate(user)

        first_response = client.get(url)
        self.assertEqual(first_response.status_code, 200, first_response.data)
        [inventory_request] = InventoryRequest.objects.filter(
            production_plan=plan, item=self.raw, status="pending"
        )
        self.assertEqual(inventory_request.quantity, 20)

        second_response = client.get(url)
        self.assertEqual(second_response.status_code, 200, second_response.data)
        self.assertEqual(
            InventoryRequest.objects.filter(production_plan=plan, item=self.raw).count(),
            1,
        )

        request_payload = client.get("/api/inventory/requests/").data[0]
        self.assertEqual(request_payload["production_plan_id"], plan.id)
        self.assertEqual(request_payload["production_plan_number"], plan.plan_number)
        self.assertEqual(request_payload["status"], "pending")

        production_order = plan.convert_to_production_order(
            warehouse=self.warehouse,
            user=user,
        )
        inventory_request.refresh_from_db()
        self.assertEqual(inventory_request.production_order_id, production_order.id)
        self.assertEqual(
            InventoryRequest.objects.filter(production_plan=plan, item=self.raw).count(),
            1,
        )

    def test_mrp_nets_open_po_and_protects_configured_stock_and_moq(self):
        self.raw.safety_stock = 3
        self.raw.minimum_order_quantity = 8
        self.raw.lead_time_days = 5
        self.raw.save()
        bom = BOM.objects.create(finished_good=self.finished, version="1.0", is_active=True)
        BOMLine.objects.create(bom=bom, raw_material=self.raw, quantity=10, unit="kg")
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=2)
        vendor = Vendor.objects.create(company=self.company, name="Vendor")
        po = PurchaseOrder.objects.create(vendor=vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.raw, quantity=4)
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=self.order, sales_order_item=self.order_item,
            customer=self.customer, item=self.finished, order_quantity=10,
            planned_quantity=1, target_date=date(2026, 12, 1),
        )

        result = calculate_mrp_for_plan(plan, warehouse=self.warehouse)
        material = result["raw_materials"][0]
        self.assertEqual(material["shortage_quantity"], 7)
        self.assertEqual(material["on_order_quantity"], 4)
        self.assertEqual(material["safety_stock"], 3)
        self.assertEqual(material["recommended_purchase_quantity"], 8)
        self.assertEqual(material["purchase_by_date"], "2026-11-26")

    def test_plan_converts_in_multiple_runs_without_overproduction(self):
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=100)
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=self.order, sales_order_item=self.order_item,
            customer=self.customer, item=self.finished, order_quantity=10,
            planned_quantity=10, status="planned",
        )

        first = plan.convert_to_production_order(warehouse=self.warehouse, quantity=4)
        plan.refresh_from_db()
        self.assertEqual(first.quantity, 4)
        self.assertEqual(plan.status, "partially_converted")
        self.assertEqual(plan.remaining_quantity, 6)

        second = plan.convert_to_production_order(warehouse=self.warehouse, quantity=6)
        plan.refresh_from_db()
        self.assertEqual(second.quantity, 6)
        self.assertEqual(plan.status, "converted")
        self.assertEqual(plan.remaining_quantity, 0)
        with self.assertRaises(ValidationError):
            plan.convert_to_production_order(warehouse=self.warehouse, quantity=1)
        self.assertEqual(ProductionOrder.objects.filter(production_plan=plan).count(), 2)

    def test_mto_plan_requires_confirmed_customer_order(self):
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=100)
        self.order.status = "pending"
        self.order.save(update_fields=["status"])
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=self.order, sales_order_item=self.order_item,
            customer=self.customer, item=self.finished, order_quantity=10,
            planned_quantity=10, status="planned",
        )

        with self.assertRaisesMessage(ValidationError, "Confirm the customer order"):
            plan.convert_to_production_order(warehouse=self.warehouse)

    def test_plan_approval_setting_blocks_conversion_until_approved(self):
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=100)
        ManufacturingSettings.objects.create(
            company=self.company, production_plan_approval_required=True
        )
        plan = ProductionPlan.objects.create(
            company=self.company, sales_order=self.order, sales_order_item=self.order_item,
            customer=self.customer, item=self.finished, order_quantity=10,
            planned_quantity=10, status="pending_approval",
        )

        with self.assertRaisesMessage(ValidationError, "must be approved"):
            plan.convert_to_production_order(warehouse=self.warehouse)

    def test_bom_is_the_execution_material_source_when_present(self):
        bom = BOM.objects.create(finished_good=self.finished, version="2.0", is_active=True)
        BOMLine.objects.create(bom=bom, raw_material=self.raw, quantity=4, unit="kg")
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=100)

        from production.execution import create_production_order, reserve_materials
        order, _ = create_production_order(self.recipe, 10, self.warehouse, validate=False)
        self.assertEqual(reserve_materials(order), [])
        ledger = order.material_requirements.get(item=self.raw)

        self.assertEqual(ledger.required_quantity, 40)
        self.assertEqual(Stock.objects.get(item=self.raw, warehouse=self.warehouse).quantity, 60)

    def test_mto_direct_production_order_api_is_blocked(self):
        user = User.objects.create_user(
            username="mto-production", password="test", role="production", company=self.company
        )
        client = APIClient()
        client.force_authenticate(user)

        response = client.post(
            "/api/production/production-orders/",
            {"recipe": self.recipe.id, "quantity": 5, "warehouse": self.warehouse.id},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("production_plan", response.data)
        self.assertFalse(ProductionOrder.objects.exists())

    def test_confirmed_mto_mark_ready_creates_plan_not_production_order(self):
        user = User.objects.create_user(
            username="mto-sales", password="test", role="sales", company=self.company
        )
        client = APIClient()
        client.force_authenticate(user)

        response = client.post(
            f"/api/sales/sales-orders/{self.order.id}/mark_ready_for_production/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(ProductionPlan.objects.filter(sales_order=self.order).exists())
        self.assertFalse(ProductionOrder.objects.exists())
