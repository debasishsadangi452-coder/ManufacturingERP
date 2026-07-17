"""Tests for the Level 1 ERP -> QuickBooks push integration.

The QuickBooks HTTP boundary (services._open_json) is mocked with a fake
QuickBooks Online server that assigns Ids and echoes entities back, so these
tests exercise everything except the network: payload construction, dependency
ordering (customer/items before invoices), Id/SyncToken bookkeeping, the
auto-push signals, and error capture.
"""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Company
from inventory.models import Item, Stock, Warehouse
from procurement.models import Bill, BillLine, PurchaseOrder, PurchaseOrderItem, Vendor
from sales.models import Customer, CustomerPayment, Invoice, InvoiceLine, SalesOrder, SalesOrderItem

from . import push, services
from .models import QuickBooksConnection, QuickBooksSyncError


TEST_QB_CONFIG = {
    "CLIENT_ID": "test-client",
    "CLIENT_SECRET": "test-secret",
    "REDIRECT_URI": "http://localhost:8000/api/quickbooks/callback/",
    "ENVIRONMENT": "sandbox",
    "FRONTEND_REDIRECT_URI": "http://localhost:5173/settings",
}

_ACCOUNTS = {
    "Income": {"Id": "79", "AccountType": "Income", "AccountSubType": "SalesOfProductIncome"},
    "Cost of Goods Sold": {"Id": "80", "AccountType": "Cost of Goods Sold", "AccountSubType": "SuppliesMaterialsCogs"},
    "Other Current Asset": {"Id": "81", "AccountType": "Other Current Asset", "AccountSubType": "Inventory"},
    "Accounts Payable": {"Id": "33", "AccountType": "Accounts Payable", "AccountSubType": "AccountsPayable"},
}


class FakeQuickBooks:
    """Answers services._open_json calls like a QuickBooks Online sandbox."""

    def __init__(self):
        self.requests = []  # (resource, body) for entity writes
        self.next_id = 100

    def __call__(self, request):
        url = request.full_url
        parts = urlsplit(url)
        if parts.path.endswith("/query"):
            query = unquote(parse_qs(parts.query)["query"][0])
            if "from Account" in query:
                for account_type, account in _ACCOUNTS.items():
                    if f"'{account_type}'" in query:
                        return {"QueryResponse": {"Account": [account]}}
            return {"QueryResponse": {}}

        # GETs (companyinfo, entity reads) carry no body.
        if request.data is None:
            if "/companyinfo/" in parts.path:
                return {"CompanyInfo": {"CompanyName": "FreshFizz Test Co"}}
            return {}

        resource = parts.path.rstrip("/").split("/")[-1]
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append((resource, body))
        key = push._RESPONSE_KEYS[resource]
        entity = dict(body)
        if "Id" not in entity:
            self.next_id += 1
            entity["Id"] = str(self.next_id)
        entity["SyncToken"] = str(int(body.get("SyncToken", "-1")) + 1)
        return {key: entity}

    def last_body(self, resource):
        bodies = [b for r, b in self.requests if r == resource]
        return bodies[-1] if bodies else None


@override_settings(QUICKBOOKS_CONFIG=TEST_QB_CONFIG)
class PushTestCase(TestCase):
    def setUp(self):
        self.fake = FakeQuickBooks()
        patcher = mock.patch("quickbooks.services._open_json", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

        with push.suppress_auto_push():
            self.company = Company.objects.create(name="FreshFizz Test Co")
        self.connection = QuickBooksConnection.objects.create(
            company=self.company,
            realm_id="12345",
            environment="sandbox",
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        self.connection.set_access_token("token")
        self.connection.set_refresh_token("refresh")
        self.connection.save()

    def _make_masters(self):
        with push.suppress_auto_push():
            customer = Customer.objects.create(
                company=self.company, name="ABC Industries", email="abc@example.com"
            )
            vendor = Vendor.objects.create(company=self.company, name="XYZ Plastics")
            item = Item.objects.create(
                company=self.company,
                name="Premium Water 500ml",
                category="finished_good",
                selling_price=Decimal("20.00"),
                sku="FG-001",
            )
        return customer, vendor, item

    def test_push_customer_creates_and_updates(self):
        customer, _, _ = self._make_masters()
        push.push_customer(self.connection, customer)
        customer.refresh_from_db()
        self.assertTrue(customer.quickbooks_id)
        body = self.fake.last_body("customer")
        self.assertEqual(body["DisplayName"], "ABC Industries")
        self.assertEqual(body["PrimaryEmailAddr"], {"Address": "abc@example.com"})

        # Second push becomes a sparse update carrying Id + SyncToken
        push.push_customer(self.connection, customer)
        body = self.fake.last_body("customer")
        self.assertEqual(body["Id"], customer.quickbooks_id)
        self.assertTrue(body["sparse"])

    def test_push_item_as_tracked_inventory(self):
        _, _, item = self._make_masters()
        with push.suppress_auto_push():
            warehouse = Warehouse.objects.create(company=self.company, name="Main", location="HQ")
            Stock.objects.create(item=item, warehouse=warehouse, quantity=1100)
        push.push_item(self.connection, item)
        body = self.fake.last_body("item")
        self.assertEqual(body["Type"], "Inventory")
        self.assertEqual(body["QtyOnHand"], 1100)
        self.assertEqual(body["IncomeAccountRef"], {"value": "79"})
        item.refresh_from_db()
        self.assertTrue(item.quickbooks_id)

    def test_push_raw_material_as_quickbooks_inventory_purchase_item(self):
        with push.suppress_auto_push():
            item = Item.objects.create(
                company=self.company,
                name="Cane Sugar",
                category="raw_material",
                unit="kg",
                selling_price=Decimal("99.00"),
                purchase_cost=Decimal("12.50"),
                sku="RM-SUGAR",
            )
            warehouse = Warehouse.objects.create(company=self.company, name="Raw Store", location="HQ")
            Stock.objects.create(item=item, warehouse=warehouse, quantity=250)

        push.push_item(self.connection, item)

        body = self.fake.last_body("item")
        self.assertEqual(body["Type"], "Inventory")
        self.assertEqual(body["TrackQtyOnHand"], True)
        self.assertEqual(body["QtyOnHand"], 250)
        self.assertEqual(body["UnitPrice"], 0)
        self.assertEqual(body["PurchaseCost"], 12.5)
        self.assertIn("purchase from vendors only", body["Description"])

    def test_pull_quickbooks_inventory_item_as_raw_material(self):
        payload = {
            "Id": "qb-raw-1",
            "SyncToken": "0",
            "Type": "Inventory",
            "Name": "QuickBooks Sugar",
            "UnitPrice": 0,
            "PurchaseCost": 7.25,
            "Sku": "QB-SUGAR",
        }

        created = services._upsert_item(self.connection, payload)

        item = Item.objects.get(company=self.company, quickbooks_id="qb-raw-1")
        self.assertTrue(created)
        self.assertEqual(item.category, "raw_material")
        self.assertEqual(item.purchase_cost, Decimal("7.25"))

    def test_pull_quickbooks_estimate_as_sales_order(self):
        customer, _, item = self._make_masters()
        customer.quickbooks_id = "qb-customer-1"
        customer.save(update_fields=["quickbooks_id"])
        item.quickbooks_id = "qb-item-1"
        item.save(update_fields=["quickbooks_id"])

        created = services._upsert_estimate(
            self.connection,
            {
                "Id": "qb-estimate-1",
                "SyncToken": "0",
                "CustomerRef": {"value": "qb-customer-1", "name": customer.name},
                "TxnStatus": "Accepted",
                "TotalAmt": 125,
                "Line": [
                    {
                        "Description": item.name,
                        "Amount": 125,
                        "SalesItemLineDetail": {
                            "ItemRef": {"value": "qb-item-1", "name": item.name},
                            "Qty": 5,
                            "UnitPrice": 25,
                        },
                    }
                ],
            },
        )

        order = SalesOrder.objects.get(quickbooks_id="qb-estimate-1")
        self.assertTrue(created)
        self.assertEqual(order.status, "confirmed")
        self.assertEqual(order.total_amount, Decimal("125"))
        self.assertEqual(order.salesorderitem_set.count(), 1)

    def test_pull_quickbooks_invoice_and_payment_as_sales_records(self):
        customer, _, item = self._make_masters()
        customer.quickbooks_id = "qb-customer-2"
        customer.save(update_fields=["quickbooks_id"])
        item.quickbooks_id = "qb-item-2"
        item.save(update_fields=["quickbooks_id"])

        services._upsert_invoice(
            self.connection,
            {
                "Id": "qb-invoice-1",
                "SyncToken": "0",
                "CustomerRef": {"value": "qb-customer-2", "name": customer.name},
                "TxnDate": "2026-07-01",
                "DueDate": "2026-07-31",
                "TotalAmt": 200,
                "Balance": 50,
                "Line": [
                    {
                        "Description": item.name,
                        "Amount": 200,
                        "SalesItemLineDetail": {
                            "ItemRef": {"value": "qb-item-2", "name": item.name},
                            "Qty": 10,
                            "UnitPrice": 20,
                        },
                    }
                ],
            },
        )
        invoice = Invoice.objects.get(quickbooks_id="qb-invoice-1")
        self.assertEqual(invoice.status, "partial")
        self.assertEqual(invoice.amount_paid, Decimal("150"))
        self.assertEqual(invoice.lines.count(), 1)

        created = services._upsert_payment(
            self.connection,
            {
                "Id": "qb-payment-1",
                "SyncToken": "0",
                "CustomerRef": {"value": "qb-customer-2", "name": customer.name},
                "TxnDate": "2026-07-05",
                "TotalAmt": 150,
                "PaymentRefNum": "QB-PAY-1",
                "Line": [
                    {
                        "Amount": 150,
                        "LinkedTxn": [{"TxnId": "qb-invoice-1", "TxnType": "Invoice"}],
                    }
                ],
            },
        )

        payment = CustomerPayment.objects.get(quickbooks_id="qb-payment-1")
        self.assertTrue(created)
        self.assertEqual(payment.invoice, invoice)
        self.assertEqual(payment.amount, Decimal("150"))

    def test_sales_order_pushed_as_estimate_with_lines(self):
        customer, _, item = self._make_masters()
        with push.suppress_auto_push():
            order = SalesOrder.objects.create(customer=customer, total_amount=Decimal("10000"))
            SalesOrderItem.objects.create(sales_order=order, item=item, quantity=500)
        push.push_sales_order(self.connection, order)
        body = self.fake.last_body("estimate")
        self.assertEqual(body["DocNumber"], f"SO-{order.id}")
        self.assertEqual(len(body["Line"]), 1)
        line = body["Line"][0]
        self.assertEqual(line["SalesItemLineDetail"]["Qty"], 500)
        self.assertEqual(line["Amount"], 10000.0)
        # Customer and item were pushed first (dependency ordering)
        customer.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(body["CustomerRef"], {"value": customer.quickbooks_id})
        self.assertEqual(line["SalesItemLineDetail"]["ItemRef"], {"value": item.quickbooks_id})

    def test_purchase_order_push(self):
        _, vendor, item = self._make_masters()
        with push.suppress_auto_push():
            order = PurchaseOrder.objects.create(vendor=vendor, status="approved")
            PurchaseOrderItem.objects.create(
                purchase_order=order, item=item, quantity=1000, unit_price=Decimal("2.00")
            )
        push.push_purchase_order(self.connection, order)
        body = self.fake.last_body("purchaseorder")
        self.assertEqual(body["VendorRef"], {"value": vendor.quickbooks_id if vendor.quickbooks_id else body["VendorRef"]["value"]})
        self.assertEqual(body["Line"][0]["ItemBasedExpenseLineDetail"]["Qty"], 1000)
        self.assertEqual(body["Line"][0]["Amount"], 2000.0)
        order.refresh_from_db()
        self.assertTrue(order.quickbooks_id)

    def test_payment_pushes_invoice_first_and_links_it(self):
        customer, _, item = self._make_masters()
        with push.suppress_auto_push():
            invoice = Invoice.objects.create(
                company=self.company, customer=customer, total_amount=Decimal("10000")
            )
            InvoiceLine.objects.create(
                invoice=invoice, item=item, quantity=500,
                unit_price=Decimal("20.00"), amount=Decimal("10000"),
            )
            payment = CustomerPayment.objects.create(
                company=self.company, customer=customer, invoice=invoice,
                amount=Decimal("10000"), reference="NEFT-991",
            )
        push.push_payment(self.connection, payment)
        invoice.refresh_from_db()
        self.assertTrue(invoice.quickbooks_id)
        body = self.fake.last_body("payment")
        self.assertEqual(body["TotalAmt"], 10000.0)
        self.assertEqual(
            body["Line"][0]["LinkedTxn"],
            [{"TxnId": invoice.quickbooks_id, "TxnType": "Invoice"}],
        )

    def test_bill_push(self):
        _, vendor, item = self._make_masters()
        with push.suppress_auto_push():
            bill = Bill.objects.create(
                company=self.company, vendor=vendor, bill_number="XYZ-77",
                bill_date=timezone.localdate(), total_amount=Decimal("2000"),
            )
            BillLine.objects.create(
                bill=bill, item=item, quantity=1000,
                unit_price=Decimal("2.00"), amount=Decimal("2000"),
            )
        push.push_bill(self.connection, bill)
        body = self.fake.last_body("bill")
        self.assertEqual(body["DocNumber"], "XYZ-77")
        bill.refresh_from_db()
        self.assertTrue(bill.quickbooks_id)

    def test_auto_push_signal_on_customer_create(self):
        with self.captureOnCommitCallbacks(execute=True):
            customer = Customer.objects.create(company=self.company, name="Signal Co")
        customer.refresh_from_db()
        self.assertTrue(customer.quickbooks_id)

    def test_auto_push_skipped_without_connection(self):
        self.connection.is_active = False
        self.connection.save()
        with self.captureOnCommitCallbacks(execute=True):
            customer = Customer.objects.create(company=self.company, name="Offline Co")
        customer.refresh_from_db()
        self.assertEqual(customer.quickbooks_id, "")
        self.assertFalse(any(r == "customer" for r, _ in self.fake.requests))

    def test_push_failure_is_recorded_not_raised(self):
        customer, _, _ = self._make_masters()
        with mock.patch(
            "quickbooks.services._open_json",
            side_effect=push.QuickBooksAPIError("QuickBooks API error 500: boom"),
        ):
            ok = push.safe_push(self.connection, "customer", customer)
        self.assertFalse(ok)
        error = QuickBooksSyncError.objects.get(company=self.company)
        self.assertIn("500", error.message)

    def test_push_all_counts(self):
        customer, vendor, item = self._make_masters()
        with push.suppress_auto_push():
            order = SalesOrder.objects.create(customer=customer)
            SalesOrderItem.objects.create(sales_order=order, item=item, quantity=10)
        run = push.push_all(self.connection)
        self.assertEqual(run.status, "success")
        self.assertEqual(run.records_seen, 4)  # customer, vendor, item, sales order
        self.assertEqual(run.records_created, 4)

    def test_push_all_skips_already_linked_records(self):
        customer, vendor, item = self._make_masters()
        vendor.quickbooks_id = "existing-vendor"
        vendor.quickbooks_sync_token = "0"
        vendor.quickbooks_last_synced_at = timezone.now()
        vendor.save(update_fields=["quickbooks_id", "quickbooks_sync_token", "quickbooks_last_synced_at"])

        run = push.push_all(self.connection)

        self.assertEqual(run.status, "success")
        self.assertEqual(run.records_seen, 2)  # customer + item only
        self.assertEqual(run.records_created, 2)
        self.assertFalse(any(body.get("Id") == "existing-vendor" for _, body in self.fake.requests))


@override_settings(QUICKBOOKS_CONFIG=TEST_QB_CONFIG)
class ScheduledSyncCommandTestCase(TestCase):
    """The sync_quickbooks management command run by the 24h scheduler."""

    def setUp(self):
        self.fake = FakeQuickBooks()
        patcher = mock.patch("quickbooks.services._open_json", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, name):
        with push.suppress_auto_push():
            company = Company.objects.create(name=name)
        connection = QuickBooksConnection.objects.create(
            company=company,
            realm_id="12345",
            environment="sandbox",
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        connection.set_access_token("token")
        connection.set_refresh_token("refresh")
        connection.save()
        return company, connection

    def _run(self, **kwargs):
        out, err = StringIO(), StringIO()
        call_command("sync_quickbooks", stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_no_connections_is_a_noop(self):
        out, _ = self._run()
        self.assertIn("No active QuickBooks connections", out)

    def test_syncs_every_active_company_and_skips_inactive(self):
        self._connect("Alpha Co")
        self._connect("Beta Co")
        _, disconnected = self._connect("Gamma Co")
        disconnected.is_active = False
        disconnected.save(update_fields=["is_active"])

        out, _ = self._run()

        self.assertIn("Alpha Co: pulled", out)
        self.assertIn("Beta Co: pulled", out)
        self.assertNotIn("Gamma Co", out)
        self.assertIn("Sync complete", out)

    def test_pulls_then_pushes_pending_records(self):
        company, _ = self._connect("Alpha Co")
        with push.suppress_auto_push():
            Customer.objects.create(company=company, name="ABC Industries")

        out, _ = self._run()

        self.assertIn("Alpha Co: pushed created=1", out)
        self.assertTrue(any(r == "customer" for r, _ in self.fake.requests))

    def test_no_push_flag_pulls_only(self):
        company, _ = self._connect("Alpha Co")
        with push.suppress_auto_push():
            Customer.objects.create(company=company, name="ABC Industries")

        out, _ = self._run(no_push=True)

        self.assertNotIn("pushed", out)
        self.assertFalse(any(r == "customer" for r, _ in self.fake.requests))

    def test_company_flag_limits_scope(self):
        self._connect("Alpha Co")
        self._connect("Beta Co")

        out, _ = self._run(company_slug="betaco")

        self.assertIn("Beta Co: pulled", out)
        self.assertNotIn("Alpha Co", out)

    def test_one_failing_company_does_not_abort_the_others(self):
        """The reason each connection is isolated: an expired token on one
        tenant must not stop the nightly sync for every other tenant."""
        self._connect("Alpha Co")
        self._connect("Beta Co")

        real_open_json = self.fake

        def fail_for_alpha(request):
            # Alpha's realm is unreachable; Beta's still answers.
            if "alpha" in request.full_url:
                raise services.QuickBooksAPIError("QuickBooks API error 401: token expired")
            return real_open_json(request)

        # Give Alpha a distinct realm so the fake can single it out.
        QuickBooksConnection.objects.filter(company__name="Alpha Co").update(realm_id="alpha")

        with mock.patch("quickbooks.services._open_json", fail_for_alpha):
            # Non-zero exit so the scheduler flags the run, but only after
            # every healthy tenant has been synced.
            with self.assertRaises(CommandError) as ctx:
                out, err = self._run()

        self.assertIn("1 of 2 connection(s) failing", str(ctx.exception))

    def test_failing_sync_exits_non_zero(self):
        """A cron run that syncs nothing must not report success."""
        self._connect("Alpha Co")
        boom = mock.Mock(side_effect=services.QuickBooksAPIError("401: token expired"))
        with mock.patch("quickbooks.services._open_json", boom):
            with self.assertRaises(CommandError):
                self._run()
