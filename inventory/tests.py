from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import User
from inventory.models import BOM, BOMLine, InventoryRequest, Item, UnitOfMeasure, Warehouse
from inventory.uom import convert, UomConversionError
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


class UnitOfMeasureTests(TestCase):
    """P0-A: unit conversion and BOM-line base-unit deduction."""

    def setUp(self):
        self.g = UnitOfMeasure.objects.create(code="g", name="Gram", dimension="mass", to_base_factor=Decimal("1"), is_base=True)
        self.oz = UnitOfMeasure.objects.create(code="oz", name="Ounce", dimension="mass", to_base_factor=Decimal("28.34952312"))
        self.lb = UnitOfMeasure.objects.create(code="lb", name="Pound", dimension="mass", to_base_factor=Decimal("453.59237"))
        self.each = UnitOfMeasure.objects.create(code="each", name="Each", dimension="count", to_base_factor=Decimal("1"), is_base=True)

    def test_convert_within_mass(self):
        self.assertEqual(convert(1, self.lb, self.g), Decimal("453.59237"))
        self.assertAlmostEqual(float(convert(16, self.oz, self.lb)), 1.0, places=6)

    def test_same_unit_is_identity(self):
        self.assertEqual(convert(42, self.g, self.g), Decimal("42"))

    def test_cross_dimension_raises(self):
        with self.assertRaises(UomConversionError):
            convert(1, self.g, self.each)

    def test_missing_unit_raises(self):
        with self.assertRaises(UomConversionError):
            convert(1, self.g, None)

    def test_bomline_quantity_in_base_unit_converts(self):
        # Sugar stocked in grams; recipe calls for 2 oz → deduct 56.699g.
        sugar = Item.objects.create(name="Sugar", category="raw_material", unit="g", base_unit=self.g)
        cookie = Item.objects.create(name="Cookie", category="finished_good", unit="each")
        bom = BOM.objects.create(finished_good=cookie)
        line = BOMLine.objects.create(bom=bom, raw_material=sugar, quantity=2, unit="oz", unit_of_measure=self.oz)
        self.assertAlmostEqual(float(line.quantity_in_base_unit()), 56.6990, places=3)

    def test_bomline_falls_back_when_units_unset(self):
        # Backfill safety: no units configured → raw quantity, no crash.
        sugar = Item.objects.create(name="Sugar2", category="raw_material", unit="kg")
        cookie = Item.objects.create(name="Cookie2", category="finished_good", unit="each")
        bom = BOM.objects.create(finished_good=cookie)
        line = BOMLine.objects.create(bom=bom, raw_material=sugar, quantity=5, unit="kg")
        self.assertEqual(line.quantity_in_base_unit(), Decimal("5"))
