"""Payment status per purchase order, and paying it from the Procurement screen."""
from datetime import date
from decimal import Decimal

from rest_framework import status

from accounting.models import AccountingPeriod, JournalEntry
from accounting.test_auto_posting import AutoPostingTestBase, lines_of
from accounts.models import User
from procurement.models import Bill, PurchaseOrder, PurchaseOrderItem, VendorPayment

PO_URL = "/api/procurement/purchase-orders/"


class PurchaseOrderPaymentTests(AutoPostingTestBase):
    def received_po(self):
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.sugar, quantity=100, unit_price=Decimal("2.00"))
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post("/api/procurement/goods-receipts/",
                                   {"purchase_order": po.id, "warehouse": self.warehouse.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        return po

    def billing(self, po):
        return self.client.get(f"{PO_URL}{po.id}/").data["billing"]

    def test_status_is_shown_per_order(self):
        unbilled = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        self.assertEqual(self.billing(unbilled), {"state": "not_billed"})

        po = self.received_po()
        billing = self.billing(po)
        self.assertEqual(billing["state"], "open")
        self.assertEqual(Decimal(billing["balance_due"]), Decimal("200.00"))

    def test_admin_pays_in_full_and_the_ledger_is_updated(self):
        po = self.received_po()
        res = self.client.post(f"{PO_URL}{po.id}/pay_bill/", {"method": "bank_transfer"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(res.data["billing"]["state"], "paid")
        self.assertEqual(Bill.objects.get(purchase_order=po).status, "paid")
        entry = JournalEntry.objects.get(company=self.company, source_module="procurement.payment", status="posted")
        self.assertEqual(lines_of(entry), {
            "2010": (Decimal("200.00"), Decimal("0.00")),
            "1010": (Decimal("0.00"), Decimal("200.00")),
        })

    def test_partial_payment_then_overpayment_is_refused(self):
        po = self.received_po()
        res = self.client.post(f"{PO_URL}{po.id}/pay_bill/", {"amount": "50"}, format="json")
        self.assertEqual(res.data["billing"]["state"], "partial")
        self.assertEqual(Decimal(res.data["billing"]["balance_due"]), Decimal("150.00"))
        res = self.client.post(f"{PO_URL}{po.id}/pay_bill/", {"amount": "500"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_store_users_see_the_status_but_cannot_pay(self):
        po = self.received_po()
        store = User.objects.create_user(username="sid.store", email="sid@fizz.test", role="store",
                                         company=self.company, password="pass")
        self.client.force_authenticate(user=store)
        self.assertEqual(self.billing(po)["state"], "open")
        res = self.client.post(f"{PO_URL}{po.id}/pay_bill/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_order_without_bill_cannot_be_paid(self):
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        res = self.client.post(f"{PO_URL}{po.id}/pay_bill/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_payment_refused_by_the_ledger_is_rolled_back(self):
        po = self.received_po()
        AccountingPeriod.objects.filter(
            company=self.company, start_date__lte=date.today(), end_date__gte=date.today()
        ).update(status="closed")
        res = self.client.post(f"{PO_URL}{po.id}/pay_bill/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Bill.objects.get(purchase_order=po).status, "open")
        self.assertFalse(VendorPayment.objects.exists())
