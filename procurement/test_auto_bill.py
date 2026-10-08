"""Vendor bill created automatically when goods are received."""
from datetime import date, timedelta
from decimal import Decimal

from rest_framework import status

from accounting.models import AccountingSettings, JournalEntry
from accounting.test_auto_posting import AutoPostingTestBase, lines_of
from accounts.models import Company, User
from inventory.models import Batch, Item, Stock, UnitOfMeasure, Warehouse
from procurement.billing import payment_terms_days
from procurement.models import Bill, PurchaseOrder, PurchaseOrderItem, Vendor
from django.utils import timezone


class AutoBillOnReceiptTests(AutoPostingTestBase):
    def receive(self, po, warehouse=None):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                "/api/procurement/goods-receipts/",
                {"purchase_order": po.id, "warehouse": (warehouse or self.warehouse).id},
                format="json",
            )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

    def make_po(self, vendor=None, item=None):
        po = PurchaseOrder.objects.create(vendor=vendor or self.vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=item or self.sugar, quantity=100, unit_price=Decimal("2.00"))
        return po

    def test_receiving_goods_creates_and_posts_the_vendor_bill(self):
        self.vendor.payment_terms = "net30"
        self.vendor.save()
        po = self.make_po()
        self.receive(po)

        bill = Bill.objects.get(purchase_order=po)
        self.assertEqual(bill.total_amount, Decimal("200.00"))
        self.assertEqual(bill.lines.count(), 1)
        self.assertEqual(bill.due_date, timezone.localdate() + timedelta(days=30))
        self.assertEqual(lines_of(self.entry("procurement.receipt")), {
            "1210": (Decimal("200.00"), Decimal("0.00")),
            "2050": (Decimal("0.00"), Decimal("200.00")),
        })
        self.assertEqual(lines_of(self.entry("procurement.bill")), {
            "2050": (Decimal("200.00"), Decimal("0.00")),
            "2010": (Decimal("0.00"), Decimal("200.00")),
        })

    def test_setting_off_means_no_automatic_bill(self):
        AccountingSettings.objects.filter(company=self.company).update(auto_bill_on_receipt=False)
        po = self.make_po()
        self.receive(po)
        self.assertFalse(Bill.objects.filter(purchase_order=po).exists())

    def test_an_existing_bill_is_not_duplicated(self):
        po = self.make_po()
        Bill.objects.create(company=self.company, purchase_order=po, vendor=self.vendor,
                            bill_date=timezone.localdate(), total_amount=Decimal("200.00"))
        self.receive(po)
        self.assertEqual(Bill.objects.filter(purchase_order=po).count(), 1)

    def test_receipt_converts_purchase_quantity_to_stock_unit(self):
        gram = UnitOfMeasure.objects.create(
            company=self.company, code="g", name="Gram", dimension="mass",
            to_base_factor=Decimal("1"), is_base=True,
        )
        kilogram = UnitOfMeasure.objects.create(
            company=self.company, code="kg", name="Kilogram", dimension="mass",
            to_base_factor=Decimal("1000"),
        )
        self.sugar.base_unit = gram
        self.sugar.purchase_unit = kilogram
        self.sugar.save(update_fields=["base_unit", "purchase_unit"])
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        PurchaseOrderItem.objects.create(
            purchase_order=po, item=self.sugar, quantity=2, unit_price=Decimal("2.00")
        )

        self.receive(po)

        self.assertEqual(Stock.objects.get(item=self.sugar, warehouse=self.warehouse).quantity, 2000)
        self.assertEqual(Batch.objects.get(goods_receipt__purchase_order=po).quantity, 2000)

    def test_companies_not_using_accounting_keep_the_old_behaviour(self):
        other = Company.objects.create(name="No Books Ltd", slug="nobooks")
        user = User.objects.create_user(username="sam.store", email="s@nobooks.test", role="store",
                                        company=other, password="pass")
        vendor = Vendor.objects.create(company=other, name="Other Vendor")
        item = Item.objects.create(company=other, name="Flour", category="raw_material")
        po = self.make_po(vendor=vendor, item=item)
        warehouse = Warehouse.objects.create(company=other, name="Main", location="X")
        self.client.force_authenticate(user=user)
        self.receive(po, warehouse)
        self.assertFalse(Bill.objects.filter(purchase_order=po).exists())
        self.assertFalse(JournalEntry.objects.filter(company=other).exists())

    def test_payment_terms_parsing(self):
        self.assertEqual(payment_terms_days("net30"), 30)
        self.assertEqual(payment_terms_days("Net 45"), 45)
        self.assertEqual(payment_terms_days("Due on receipt"), 0)
        self.assertIsNone(payment_terms_days(""))
        self.assertIsNone(payment_terms_days("custom"))
