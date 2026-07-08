from rest_framework.test import APITestCase

from accounts.models import User
from inventory.models import InventoryRequest, Item, Warehouse
from procurement.models import PurchaseOrder, Vendor, VendorPriceList


class InventoryProcurementFlowTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="store_user",
            password="pass123",
            role="store",
        )
        self.raw_item = Item.objects.create(name="Sugar Syrup", category="raw_material", unit="kg")
        self.finished_item = Item.objects.create(name="Cola 500", category="finished_good", unit="bottle")
        self.warehouse = Warehouse.objects.create(name="Main", location="Plant A")
        self.vendor = Vendor.objects.create(name="Demo Vendor")
        VendorPriceList.objects.create(
            vendor=self.vendor,
            item=self.raw_item,
            unit_price="10.50",
            min_order_qty=25,
            is_active=True,
        )

    def test_procurement_request_creates_pending_po_and_marks_request_procuring(self):
        request = InventoryRequest.objects.create(
            item=self.raw_item,
            warehouse=self.warehouse,
            quantity=10,
            status="pending",
        )

        self.client.force_authenticate(user=self.user)
        response = self.client.post(f"/api/inventory/requests/{request.id}/procure/")

        self.assertEqual(response.status_code, 200)
        request.refresh_from_db()
        po = PurchaseOrder.objects.get(id=response.data["po_id"])

        self.assertEqual(request.status, "procuring")
        self.assertEqual(po.status, "pending")
        self.assertEqual(po.items.first().quantity, 25)

    def test_procurement_rejects_finished_goods(self):
        request = InventoryRequest.objects.create(
            item=self.finished_item,
            warehouse=self.warehouse,
            quantity=10,
            status="pending",
        )

        self.client.force_authenticate(user=self.user)
        response = self.client.post(f"/api/inventory/requests/{request.id}/procure/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("raw materials", response.data["error"])
