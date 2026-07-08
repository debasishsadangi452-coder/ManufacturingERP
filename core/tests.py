from rest_framework.test import APITestCase
from rest_framework import status
from accounts.models import User
from inventory.models import Item, Warehouse, Stock, Batch
from procurement.models import Vendor, PurchaseOrder
from production.models import Recipe, ProductionOrder
from quality.models import QualityCheck
from sales.models import Customer, SalesOrder


class FreshFizzERPTestSuite(APITestCase):

    def setUp(self):
        # Create user
        self.user = User.objects.create_user(
            username="admin",
            password="Admin@123",
            role="admin"
        )

        # Login to get token
        response = self.client.post(
            "/api/token/",
            {"username": "admin", "password": "Admin@123"},
            format="json"
        )

        self.token = response.data["access"]

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )

    # ==================================================
    # INVENTORY TESTS
    # ==================================================

    def test_inventory_crud(self):
        # Create Item
        item_resp = self.client.post(
            "/api/inventory/items/",
            {"name": "Sugar", "category": "raw_material"},
            format="json"
        )
        self.assertEqual(item_resp.status_code, status.HTTP_201_CREATED)
        item_id = item_resp.data["id"]

        # Create Warehouse
        wh_resp = self.client.post(
            "/api/inventory/warehouses/",
            {"name": "Main Warehouse", "location": "Plant A"},
            format="json"
        )
        self.assertEqual(wh_resp.status_code, status.HTTP_201_CREATED)
        wh_id = wh_resp.data["id"]

        # Direct stock creation is intentionally blocked (must flow through
        # procurement/production); stock levels are set via the adjust action.
        stock_resp = self.client.post(
            "/api/inventory/stock/",
            {"item": item_id, "warehouse": wh_id, "quantity": 1000},
            format="json"
        )
        self.assertEqual(stock_resp.status_code, status.HTTP_403_FORBIDDEN)

        adjust_resp = self.client.post(
            "/api/inventory/stock/adjust/",
            {"item": item_id, "warehouse": wh_id, "quantity": 1000, "reason": "Initial load"},
            format="json"
        )
        self.assertEqual(adjust_resp.status_code, status.HTTP_200_OK)

        # Create Batch
        batch_resp = self.client.post(
            "/api/inventory/batches/",
            {"item": item_id, "batch_number": "SUG-001", "quantity": 500},
            format="json"
        )
        self.assertEqual(batch_resp.status_code, status.HTTP_201_CREATED)

    # ==================================================
    # PROCUREMENT TESTS
    # ==================================================

    def test_procurement_crud(self):
        vendor_resp = self.client.post(
            "/api/procurement/vendors/",
            {"name": "SweetSugar Ltd", "contact": "9999999999"},
            format="json"
        )
        self.assertEqual(vendor_resp.status_code, status.HTTP_201_CREATED)
        vendor_id = vendor_resp.data["id"]

        po_resp = self.client.post(
            "/api/procurement/purchase-orders/",
            {"vendor": vendor_id},
            format="json"
        )
        self.assertEqual(po_resp.status_code, status.HTTP_201_CREATED)

    # ==================================================
    # PRODUCTION TESTS
    # ==================================================

    def test_production_crud(self):
        item = Item.objects.create(name="Orange Soda", category="finished_good")
        warehouse = Warehouse.objects.create(name="Plant WH", location="Plant A")

        recipe_resp = self.client.post(
            "/api/production/recipes/",
            {"product": item.id},
            format="json"
        )
        self.assertEqual(recipe_resp.status_code, status.HTTP_201_CREATED)
        recipe_id = recipe_resp.data["id"]

        prod_resp = self.client.post(
            "/api/production/production-orders/",
            {"recipe": recipe_id, "quantity": 500, "warehouse": warehouse.id, "status": "scheduled"},
            format="json"
        )
        self.assertEqual(prod_resp.status_code, status.HTTP_201_CREATED)

    # ==================================================
    # QUALITY TESTS
    # ==================================================

    def test_quality_crud(self):
        item = Item.objects.create(name="Juice", category="finished_good")
        warehouse = Warehouse.objects.create(name="QC WH", location="Plant B")
        recipe = Recipe.objects.create(product=item)
        prod = ProductionOrder.objects.create(recipe=recipe, quantity=100, warehouse=warehouse)

        qc_resp = self.client.post(
            "/api/quality/quality-checks/",
            {
                "production_order": prod.id,
                "status": "approved",
                "remarks": "All tests passed"
            },
            format="json"
        )
        self.assertEqual(qc_resp.status_code, status.HTTP_201_CREATED)

    # ==================================================
    # SALES TESTS
    # ==================================================

    def test_sales_crud(self):
        cust_resp = self.client.post(
            "/api/sales/customers/",
            {"name": "CityMart Distributor", "contact": "8888888888"},
            format="json"
        )
        self.assertEqual(cust_resp.status_code, status.HTTP_201_CREATED)
        cust_id = cust_resp.data["id"]

        order_resp = self.client.post(
            "/api/sales/sales-orders/",
            {"customer": cust_id},
            format="json"
        )
        self.assertEqual(order_resp.status_code, status.HTTP_201_CREATED)

    # ==================================================
    # AUTH TEST
    # ==================================================

    def test_unauthorized_access(self):
        self.client.credentials()  # remove token

        response = self.client.get("/api/inventory/items/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
