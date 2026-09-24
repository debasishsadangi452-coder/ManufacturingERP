"""End-to-end tests for GL auto-posting: operational endpoints -> journal entries."""
from datetime import date
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item, Stock, Warehouse
from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor
from production.models import ProductionOrder, Recipe, RecipeIngredient
from sales.models import Customer, SalesOrder, SalesOrderItem

from .auto_posting import execute_auto_post
from .models import AccountingSettings, AutoPostingLog, FiscalYear, JournalEntry
from .seeds import seed_standard_chart_of_accounts, seed_standard_fiscal_year


def lines_of(entry):
    """{account code: (debit, credit)} summed per account."""
    result = {}
    for line in entry.lines.select_related("account"):
        dr, cr = result.get(line.account.code, (Decimal("0"), Decimal("0")))
        result[line.account.code] = (dr + line.debit, cr + line.credit)
    return result


class AutoPostingTestBase(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
        self.admin = User.objects.create_user(
            username="admin.fizz", email="admin@fizz.test", role="admin",
            company=self.company, password="pass",
        )
        seed_standard_chart_of_accounts(self.company)
        seed_standard_fiscal_year(self.company, date.today().year)

        self.warehouse = Warehouse.objects.create(company=self.company, name="Plant", location="HQ")
        self.sugar = Item.objects.create(
            company=self.company, name="Sugar", category="raw_material", purchase_cost=Decimal("2.00"),
        )
        self.soda = Item.objects.create(
            company=self.company, name="Soda", category="finished_good", selling_price=Decimal("5.00"),
        )
        # 5 kg sugar per batch of 10 bottles -> standard cost 1.00 per bottle
        self.recipe = Recipe.objects.create(product=self.soda, batch_size=10)
        RecipeIngredient.objects.create(recipe=self.recipe, item=self.sugar, quantity=5)

        self.vendor = Vendor.objects.create(company=self.company, name="Sweet Supplies")
        self.customer = Customer.objects.create(company=self.company, name="Corner Store")
        self.client.force_authenticate(user=self.admin)

    def entry(self, source_module):
        return JournalEntry.objects.get(company=self.company, source_module=source_module, status="posted")


class PurchaseCycleTests(AutoPostingTestBase):
    def test_goods_receipt_then_bill_capitalises_inventory_and_clears_grni(self):
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.sugar, quantity=100, unit_price=Decimal("2.00"))

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post("/api/procurement/goods-receipts/",
                                   {"purchase_order": po.id, "warehouse": self.warehouse.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(lines_of(self.entry("procurement.receipt")), {
            "1210": (Decimal("200.00"), Decimal("0.00")),
            "2050": (Decimal("0.00"), Decimal("200.00")),
        })

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post("/api/procurement/bills/from_purchase_order/",
                                   {"purchase_order": po.id, "bill_number": "SS-1"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(lines_of(self.entry("procurement.bill")), {
            "2050": (Decimal("200.00"), Decimal("0.00")),
            "2010": (Decimal("0.00"), Decimal("200.00")),
        })


class ProductionAndSalesCycleTests(AutoPostingTestBase):
    def test_production_shipment_invoice_and_payment_are_posted(self):
        Stock.objects.create(item=self.sugar, warehouse=self.warehouse, quantity=100)

        # Production: 20 bottles = 2 batches = 10 kg sugar = 20.00
        order = ProductionOrder.objects.create(recipe=self.recipe, quantity=20, warehouse=self.warehouse)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/production/production-orders/{order.id}/complete/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(lines_of(self.entry("production.order")), {
            "1220": (Decimal("20.00"), Decimal("20.00")),
            "1210": (Decimal("0.00"), Decimal("20.00")),
            "1230": (Decimal("20.00"), Decimal("0.00")),
        })

        # Shipment: 20 bottles at standard cost 1.00 -> COGS 20.00
        so = SalesOrder.objects.create(customer=self.customer, status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=20)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/fulfill_order/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(lines_of(self.entry("sales.shipment")), {
            "5050": (Decimal("20.00"), Decimal("0.00")),
            "1230": (Decimal("0.00"), Decimal("20.00")),
        })

        # Invoice: 20 x 5.00 = 100.00
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/generate_invoice/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        invoice_id = res.data["id"]
        self.assertEqual(lines_of(self.entry("sales.invoice")), {
            "1100": (Decimal("100.00"), Decimal("0.00")),
            "4010": (Decimal("0.00"), Decimal("100.00")),
        })

        # Payment: 40.00 received
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/invoices/{invoice_id}/record_payment/",
                                   {"amount": "40.00"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(lines_of(self.entry("sales.payment")), {
            "1010": (Decimal("40.00"), Decimal("0.00")),
            "1100": (Decimal("0.00"), Decimal("40.00")),
        })

        self.assertEqual(AutoPostingLog.objects.filter(company=self.company, status="posted").count(), 4)


class SafetyTests(AutoPostingTestBase):
    def make_order(self):
        so = SalesOrder.objects.create(customer=self.customer, status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=2)
        return so

    def test_disabled_setting_posts_nothing(self):
        AccountingSettings.objects.filter(company=self.company).update(auto_post_enabled=False)
        so = self.make_order()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/generate_invoice/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertFalse(JournalEntry.objects.filter(company=self.company).exists())
        self.assertFalse(AutoPostingLog.objects.exists())

    def test_company_without_accounting_setup_is_untouched(self):
        other = Company.objects.create(name="No Books Ltd", slug="nobooks")
        user = User.objects.create_user(username="admin.nobooks", email="a@nobooks.test", role="admin",
                                        company=other, password="pass")
        item = Item.objects.create(company=other, name="Widget", category="finished_good",
                                   selling_price=Decimal("3.00"))
        so = SalesOrder.objects.create(customer=Customer.objects.create(company=other, name="Buyer"),
                                       status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=item, quantity=1)
        self.client.force_authenticate(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/generate_invoice/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertFalse(AutoPostingLog.objects.exists())

    def test_failure_never_breaks_the_operation_and_can_be_retried(self):
        # No fiscal year -> no open period -> posting must fail, invoice must still be created.
        FiscalYear.objects.filter(company=self.company).delete()

        so = self.make_order()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/generate_invoice/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        log = AutoPostingLog.objects.get(company=self.company, event="sales_invoice")
        self.assertEqual(log.status, "failed")
        self.assertIn("period", log.message.lower())

        seed_standard_fiscal_year(self.company, date.today().year)
        res = self.client.post(f"/api/accounting/auto-posting/{log.id}/retry/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(res.data["status"], "posted")
        self.assertEqual(res.data["attempts"], 2)
        self.assertTrue(self.entry("sales.invoice"))

    def test_same_event_is_never_posted_twice(self):
        so = self.make_order()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(f"/api/sales/sales-orders/{so.id}/generate_invoice/")
        log = execute_auto_post("sales_invoice", self.company, res.data["id"], user=self.admin)
        self.assertEqual(log.status, "skipped")
        self.assertEqual(JournalEntry.objects.filter(company=self.company, source_module="sales.invoice").count(), 1)

    def test_settings_endpoint_exposes_toggle(self):
        res = self.client.post("/api/accounting/settings/current/", {"auto_post_enabled": False}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data["auto_post_enabled"])
