from django.test import TestCase

from inventory.models import Item

from .models import PurchaseOrder, Vendor
from .serializers import PurchaseOrderItemSerializer, VendorPriceListSerializer


class ProcurementRawMaterialValidationTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(name="Packaging Supplier")
        self.raw_item = Item.objects.create(name="Aluminum Can", category="raw_material", unit="pcs")
        self.finished_item = Item.objects.create(name="Sparkling Water", category="finished_good", unit="bottle")
        self.purchase_order = PurchaseOrder.objects.create(vendor=self.vendor)

    def test_vendor_price_list_only_accepts_raw_materials(self):
        serializer = VendorPriceListSerializer(
            data={
                "vendor": self.vendor.id,
                "item": self.finished_item.id,
                "unit_price": "2.50",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Only raw materials", str(serializer.errors["item"][0]))

    def test_purchase_order_line_only_accepts_raw_materials(self):
        serializer = PurchaseOrderItemSerializer(
            data={
                "purchase_order": self.purchase_order.id,
                "item": self.finished_item.id,
                "quantity": 10,
                "unit_price": "2.50",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Only raw materials", str(serializer.errors["item"][0]))

    def test_purchase_order_line_accepts_raw_materials(self):
        serializer = PurchaseOrderItemSerializer(
            data={
                "purchase_order": self.purchase_order.id,
                "item": self.raw_item.id,
                "quantity": 10,
                "unit_price": "2.50",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
