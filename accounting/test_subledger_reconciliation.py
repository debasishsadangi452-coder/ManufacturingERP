"""GL auto-posting and the #13-#15 subledgers never book the same event twice."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from rest_framework import status

from accounts.models import Company
from inventory.models import Stock, StockMovement
from inventory.serializers import StockMovementSerializer
from procurement.models import Bill, PurchaseOrder, PurchaseOrderItem
from sales.models import SalesOrder, SalesOrderItem

from .inventory_accounting import determine_movement_accounting_requirement, post_inventory_movement_to_accounting
from .models import JournalEntry
from .purchase_accounting import post_purchase_to_accounting
from .test_auto_posting import AutoPostingTestBase, lines_of
from django.utils import timezone


class SubledgerReconciliationTests(AutoPostingTestBase):
    def receive_po(self):
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.sugar, quantity=100, unit_price=Decimal("2.00"))
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post("/api/procurement/goods-receipts/",
                                   {"purchase_order": po.id, "warehouse": self.warehouse.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        return po

    def test_auto_posted_goods_receipt_is_not_posted_again_from_inventory(self):
        po = self.receive_po()
        movement = StockMovement.objects.get(reference=f"GRN PO#{po.id}")
        required, reason, subtype = determine_movement_accounting_requirement(movement, self.company)
        self.assertFalse(required)
        self.assertEqual(subtype, "auto_posted")
        with self.assertRaises(ValidationError):
            post_inventory_movement_to_accounting(movement.id, self.admin, self.company)

    def test_manual_bill_posting_clears_grni_after_auto_posted_receipt(self):
        po = self.receive_po()
        bill = Bill.objects.create(  # created directly, so only the manual path posts it
            company=self.company, purchase_order=po, vendor=self.vendor,
            bill_date=timezone.localdate(), total_amount=po.total_amount,
        )
        entry = post_purchase_to_accounting(bill.id, self.admin, self.company)
        self.assertEqual(lines_of(entry), {
            "2050": (Decimal("200.00"), Decimal("0.00")),
            "2010": (Decimal("0.00"), Decimal("200.00")),
        })

    def test_auto_posted_shipment_is_not_posted_again_but_reservations_still_are(self):
        Stock.objects.create(item=self.soda, warehouse=self.warehouse, quantity=50)
        Stock.objects.create(item=self.sugar, warehouse=self.warehouse, quantity=50)
        so = SalesOrder.objects.create(customer=self.customer, status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=10)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/fulfill_order/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertTrue(JournalEntry.objects.filter(source_module="sales.shipment", source_id=so.id).exists())

        shipped = StockMovement.objects.get(reference=f"Fulfilled SO#{so.id}")
        self.assertFalse(determine_movement_accounting_requirement(shipped, self.company)[0])

        # Material reserved for production of the same order is a different
        # event and must still be accountable from the Inventory tab.
        reserved = StockMovement.objects.create(
            item=self.sugar, warehouse=self.warehouse, movement_type="OUT", quantity=5,
            reference=f"Reserved for SO#{so.id} production",
        )
        self.assertTrue(determine_movement_accounting_requirement(reserved, self.company)[0])

    def test_movement_accounting_status_ignores_other_companies_entries(self):
        po = self.receive_po()
        movement = StockMovement.objects.get(reference=f"GRN PO#{po.id}")
        other = Company.objects.create(name="Other Co", slug="otherco")
        JournalEntry.objects.create(
            company=other, transaction_date=timezone.localdate(), reference="X", description="other tenant",
            source_module="inventory", source_id=movement.id, status="draft",
        )
        data = StockMovementSerializer(movement).data
        self.assertIsNone(data["journal_entry_id"])
