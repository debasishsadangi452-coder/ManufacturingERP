from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item, Stock, Warehouse
from production.models import ProductionMaterialRequirement, ProductionOrder, Recipe, RecipeIngredient
from quality.models import QualityCheck
from sales.models import Customer, InvoiceLine, SalesOrder, SalesOrderItem


class TrackBIntegrityTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Track B Plant", slug="track-b")
        self.user = User.objects.create_user(
            username="admin.trackb",
            password="pass123",
            role="admin",
            company=self.company,
        )
        self.client.force_authenticate(self.user)
        self.warehouse = Warehouse.objects.create(company=self.company, name="Main", location="Plant")
        self.customer = Customer.objects.create(company=self.company, name="Customer A")
        self.raw = Item.objects.create(company=self.company, name="Steel", category="raw_material", unit="kg")
        self.fg = Item.objects.create(
            company=self.company,
            name="Bracket",
            category="finished_good",
            unit="ea",
            selling_price=Decimal("500.00"),
        )
        self.recipe = Recipe.objects.create(product=self.fg, batch_size=1)
        RecipeIngredient.objects.create(recipe=self.recipe, item=self.raw, quantity=100)

    def test_partial_material_reserved_is_not_deducted_again_on_completion(self):
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=60)
        order = SalesOrder.objects.create(customer=self.customer, status="pending")
        SalesOrderItem.objects.create(sales_order=order, item=self.fg, quantity=1)

        res = self.client.post(f"/api/sales/sales-orders/{order.id}/mark_ready_for_production/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        prod = ProductionOrder.objects.get(sales_order=order)
        ledger = ProductionMaterialRequirement.objects.get(production_order=prod, item=self.raw)
        self.assertEqual(ledger.required_quantity, 100)
        self.assertEqual(ledger.consumed_quantity, 60)
        self.assertEqual(ledger.shortage_quantity, 40)
        self.assertEqual(Stock.objects.get(item=self.raw, warehouse=self.warehouse).quantity, 0)

        stock = Stock.objects.get(item=self.raw, warehouse=self.warehouse)
        stock.quantity = 40
        stock.save()

        res = self.client.post(f"/api/production/production-orders/{prod.id}/complete/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        ledger.refresh_from_db()
        self.assertEqual(ledger.consumed_quantity, 100)
        self.assertEqual(ledger.shortage_quantity, 0)
        self.assertEqual(Stock.objects.get(item=self.raw, warehouse=self.warehouse).quantity, 0)

    def test_finished_goods_are_not_sellable_until_qa_approval(self):
        Stock.objects.create(item=self.raw, warehouse=self.warehouse, quantity=100)
        order = SalesOrder.objects.create(customer=self.customer, status="pending")
        SalesOrderItem.objects.create(sales_order=order, item=self.fg, quantity=1)
        self.client.post(f"/api/sales/sales-orders/{order.id}/mark_ready_for_production/")
        prod = ProductionOrder.objects.get(sales_order=order)

        res = self.client.post(f"/api/production/production-orders/{prod.id}/complete/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertFalse(Stock.objects.filter(item=self.fg, warehouse=self.warehouse).exists())

        res = self.client.post(f"/api/sales/sales-orders/{order.id}/fulfill_order/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        qc = QualityCheck.objects.get(production_order=prod)
        res = self.client.post(f"/api/quality/quality-checks/{qc.id}/approve/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(Stock.objects.get(item=self.fg, warehouse=self.warehouse).quantity, 1)

        res = self.client.post(f"/api/sales/sales-orders/{order.id}/fulfill_order/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

    def test_invoice_uses_sales_order_line_price_not_current_item_price(self):
        order = SalesOrder.objects.create(customer=self.customer, status="confirmed")
        SalesOrderItem.objects.create(sales_order=order, item=self.fg, quantity=2)
        self.fg.selling_price = Decimal("550.00")
        self.fg.save(update_fields=["selling_price"])

        res = self.client.post(f"/api/sales/sales-orders/{order.id}/generate_invoice/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        line = InvoiceLine.objects.get(invoice_id=res.data["id"])
        self.assertEqual(line.unit_price, Decimal("500.00"))
        self.assertEqual(line.amount, Decimal("1000.00"))
