"""Receiving part of a purchase order, and unit conversion across departments."""
from decimal import Decimal

from rest_framework import status

from accounting.test_auto_posting import AutoPostingTestBase, lines_of
from inventory.models import Stock, UnitOfMeasure
from inventory.uom import UomConversionError, convert_by_code
from procurement.models import Bill, PurchaseOrder, PurchaseOrderItem


class SplitReceiptTests(AutoPostingTestBase):
    def setUp(self):
        super().setUp()
        self.po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        self.line = PurchaseOrderItem.objects.create(
            purchase_order=self.po, item=self.sugar, quantity=1000, unit_price=Decimal("2.00"))

    def receive(self, lines=None):
        body = {"purchase_order": self.po.id, "warehouse": self.warehouse.id}
        if lines is not None:
            body["lines"] = lines
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/procurement/goods-receipts/", body, format="json")

    def stock(self):
        return Stock.objects.filter(item=self.sugar, warehouse=self.warehouse).first()

    def test_receive_500_of_1000_keeps_order_open_until_cleared(self):
        res = self.receive([{"po_item": self.line.id, "quantity": 500}])
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.po.refresh_from_db()
        self.line.refresh_from_db()
        self.assertEqual(self.po.status, "partially_received")
        self.assertEqual(self.line.received_quantity, 500)
        self.assertEqual(self.stock().quantity, 500)
        self.assertFalse(Bill.objects.filter(purchase_order=self.po).exists())

        res = self.receive([{"po_item": self.line.id, "quantity": 500}])
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, "received")
        self.assertEqual(self.stock().quantity, 1000)
        bill = Bill.objects.get(purchase_order=self.po)
        self.assertEqual(bill.total_amount, Decimal("2000.00"))

    def test_each_receipt_posts_only_its_own_value(self):
        self.receive([{"po_item": self.line.id, "quantity": 250}])
        from accounting.models import JournalEntry
        entry = JournalEntry.objects.get(company=self.company, source_module="procurement.receipt")
        self.assertEqual(lines_of(entry)["1210"], (Decimal("500.00"), Decimal("0.00")))

    def test_cannot_receive_more_than_outstanding(self):
        self.receive([{"po_item": self.line.id, "quantity": 600}])
        res = self.receive([{"po_item": self.line.id, "quantity": 600}])
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.line.refresh_from_db()
        self.assertEqual(self.line.received_quantity, 600)

    def test_no_lines_receives_everything_outstanding(self):
        self.receive([{"po_item": self.line.id, "quantity": 400}])
        res = self.receive()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, "received")
        self.assertEqual(self.stock().quantity, 1000)


class UnitConversionTests(AutoPostingTestBase):
    def test_volume_weight_and_count_units_convert_within_dimension(self):
        self.assertAlmostEqual(float(convert_by_code(1, "gal", "l")), 3.785411784, places=6)
        self.assertAlmostEqual(float(convert_by_code(1000, "ml", "l")), 1.0, places=6)
        self.assertAlmostEqual(float(convert_by_code(1, "kg", "lb")), 2.2046226, places=5)
        self.assertAlmostEqual(float(convert_by_code(2, "kg", "mg")), 2_000_000, places=3)
        self.assertEqual(convert_by_code(2, "dozen", "each"), Decimal("24"))

    def test_cross_dimension_conversion_is_refused(self):
        with self.assertRaises(UomConversionError):
            convert_by_code(1, "kg", "l")
        res = self.client.get("/api/inventory/units/convert/", {"qty": 1, "from": "kg", "to": "l"})
        self.assertEqual(res.status_code, 400)

    def test_convert_endpoint(self):
        res = self.client.get("/api/inventory/units/convert/", {"qty": 2, "from": "kg", "to": "lb"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertAlmostEqual(res.data["quantity"], 4.40924524, places=5)
