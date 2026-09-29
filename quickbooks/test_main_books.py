"""QuickBooks as the main books: payments, ERP-only journal entries, deletions,
account mapping and the Accounting > QuickBooks API."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.engine import post_journal_entry
from accounting.models import Account, AccountingPeriod, JournalEntry, JournalEntryLine
from accounting.payables import record_and_allocate_ap_payment
from accounting.receivables import record_and_allocate_ar_payment
from accounting.seeds import seed_standard_chart_of_accounts, seed_standard_fiscal_year
from accounts.models import Company, User
from procurement.models import Bill, Vendor, VendorPayment
from sales.models import Customer, CustomerPayment, Invoice

from . import push
from .models import QuickBooksAccountMapping, QuickBooksConnection, QuickBooksSyncError
from .tests import TEST_QB_CONFIG, FakeQuickBooks

QB_CHART = [
    {"Id": "35", "Name": "Checking", "AccountType": "Bank"},
    {"Id": "84", "Name": "Accounts Receivable (A/R)", "AccountType": "Accounts Receivable"},
    {"Id": "33", "Name": "Accounts Payable (A/P)", "AccountType": "Accounts Payable"},
    {"Id": "79", "Name": "Sales of Product Income", "AccountType": "Income", "AccountSubType": "SalesOfProductIncome"},
    {"Id": "80", "Name": "Cost of Goods Sold", "AccountType": "Cost of Goods Sold", "AccountSubType": "SuppliesMaterialsCogs"},
    {"Id": "81", "Name": "Inventory Asset", "AccountType": "Other Current Asset", "AccountSubType": "Inventory"},
    {"Id": "7", "Name": "Rent or Lease", "AccountType": "Expense"},
    {"Id": "3", "Name": "Opening Balance Equity", "AccountType": "Equity"},
]


class ChartFakeQuickBooks(FakeQuickBooks):
    """FakeQuickBooks that also answers chart-of-accounts queries and records
    the full URL of every write (to see ?operation=delete)."""

    def __init__(self, chart=QB_CHART):
        super().__init__()
        self.chart = chart
        self.write_urls = []

    def __call__(self, request):
        parts = urlsplit(request.full_url)
        if parts.path.endswith("/query"):
            query = unquote(parse_qs(parts.query)["query"][0])
            if "from Account" in query:
                if "AccountType = '" in query:
                    wanted = query.split("AccountType = '")[1].split("'")[0]
                    return {"QueryResponse": {"Account": [a for a in self.chart if a["AccountType"] == wanted]}}
                return {"QueryResponse": {"Account": list(self.chart)}}
        if request.data is not None:
            self.write_urls.append(request.full_url)
        return super().__call__(request)


@override_settings(QUICKBOOKS_CONFIG=TEST_QB_CONFIG)
class MainBooksTests(TestCase):
    def setUp(self):
        self.fake = ChartFakeQuickBooks()
        patcher = mock.patch("quickbooks.services._open_json", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        push._local.__dict__.clear()  # drop cached QB account lookups between tests

        with push.suppress_auto_push():
            self.company = Company.objects.create(name="Books Co", slug="booksco")
            self.admin = User.objects.create_user(
                username="ada.admin", email="ada@books.test", role="admin", company=self.company, password="pass",
            )
            self.customer = Customer.objects.create(company=self.company, name="Corner Store")
            self.vendor = Vendor.objects.create(company=self.company, name="Sweet Supplies")
        seed_standard_chart_of_accounts(self.company)
        seed_standard_fiscal_year(self.company, timezone.localdate().year)
        self.connection = QuickBooksConnection.objects.create(
            company=self.company, realm_id="12345", environment="sandbox",
            access_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        self.connection.set_access_token("token")
        self.connection.set_refresh_token("refresh")
        self.connection.save()

    def bill(self, total="200.00"):
        with push.suppress_auto_push():
            return Bill.objects.create(company=self.company, vendor=self.vendor, bill_date=timezone.localdate(),
                                       total_amount=Decimal(total))

    def invoice(self, total="100.00"):
        with push.suppress_auto_push():
            return Invoice.objects.create(company=self.company, customer=self.customer, total_amount=Decimal(total))

    def manual_entry(self, debit_code, credit_code, amount="50.00", source="manual"):
        entry = JournalEntry.objects.create(
            company=self.company, transaction_date=timezone.localdate(), reference="M-1",
            description="Office rent", source_module=source, created_by=self.admin, status="draft",
        )
        for n, (code, dr, cr) in enumerate([(debit_code, amount, "0"), (credit_code, "0", amount)], start=1):
            JournalEntryLine.objects.create(
                company=self.company, journal_entry=entry, line_number=n,
                account=Account.objects.get(company=self.company, code=code),
                debit=Decimal(dr), credit=Decimal(cr),
            )
        with self.captureOnCommitCallbacks(execute=True):
            return post_journal_entry(entry.id, self.admin, company=self.company)

    # --- payments ---------------------------------------------------------

    def test_paying_a_bill_in_accounting_sends_a_linked_bill_payment(self):
        bill = self.bill()
        with self.captureOnCommitCallbacks(execute=True):
            record_and_allocate_ap_payment(self.vendor.id, "200.00", self.admin, self.company,
                                           allocations=[{"bill_id": bill.id, "amount": "200.00"}])
        bill.refresh_from_db()
        body = self.fake.last_body("billpayment")
        self.assertEqual(body["TotalAmt"], 200.0)
        self.assertEqual(body["Line"][0]["LinkedTxn"], [{"TxnId": bill.quickbooks_id, "TxnType": "Bill"}])
        self.assertEqual(body["CheckPayment"]["BankAccountRef"], {"value": "35"})
        self.assertTrue(VendorPayment.objects.get().quickbooks_id)

    def test_customer_payment_from_accounts_receivable_is_sent_immediately(self):
        invoice = self.invoice()
        with self.captureOnCommitCallbacks(execute=True):
            record_and_allocate_ar_payment(self.customer.id, "40.00", self.admin, self.company,
                                           allocations=[{"invoice_id": invoice.id, "amount": "40.00"}])
        self.assertEqual(self.fake.last_body("payment")["TotalAmt"], 40.0)
        self.assertTrue(CustomerPayment.objects.get().quickbooks_id)

    def test_undone_payment_is_deleted_in_quickbooks(self):
        invoice = self.invoice()
        with self.captureOnCommitCallbacks(execute=True):
            payment = CustomerPayment.objects.create(company=self.company, customer=self.customer,
                                                     invoice=invoice, amount=Decimal("40.00"))
        payment.refresh_from_db()
        self.assertTrue(payment.quickbooks_id)
        with self.captureOnCommitCallbacks(execute=True):
            payment.delete()
        self.assertTrue(any("operation=delete" in url and "/payment" in url for url in self.fake.write_urls))

    # --- journal entries --------------------------------------------------

    def test_manual_journal_entry_is_sent_with_auto_mapped_accounts(self):
        entry = self.manual_entry("6050", "1010")  # Facility Rent (expense) / Operating Bank
        entry.refresh_from_db()
        self.assertTrue(entry.quickbooks_id)
        lines = self.fake.last_body("journalentry")["Line"]
        self.assertEqual(
            [(l["JournalEntryLineDetail"]["PostingType"], l["JournalEntryLineDetail"]["AccountRef"]["value"]) for l in lines],
            [("Debit", "7"), ("Credit", "35")],
        )
        self.assertTrue(QuickBooksAccountMapping.objects.get(account__code="6050").auto_mapped)

    def test_entries_quickbooks_books_itself_are_not_sent(self):
        for source in ("sales.shipment", "manufacturing", "procurement.receipt", "inventory", "closing"):
            entry = self.manual_entry("5050", "1230", source=source)
            entry.refresh_from_db()
            self.assertFalse(entry.quickbooks_id, source)
            with self.assertRaises(ValueError):
                push.push_journal_entry(self.connection, entry)
        self.assertIsNone(self.fake.last_body("journalentry"))

    def test_unmapped_account_is_reported_with_a_clear_message(self):
        self.fake.chart = [a for a in QB_CHART if a["AccountType"] != "Expense"]
        self.manual_entry("6050", "1010")
        error = QuickBooksSyncError.objects.get(entity_type="journal_entry")
        self.assertIn("not mapped to a QuickBooks account", error.message)

    def test_entry_in_a_since_closed_period_still_stores_its_quickbooks_id(self):
        with mock.patch.object(push, "_write_entity", return_value={"Id": "9001", "SyncToken": "0"}):
            entry = self.manual_entry("6050", "1010")
        AccountingPeriod.objects.filter(company=self.company).update(status="closed")
        push.push_journal_entry(self.connection, entry)  # update path, must not raise
        entry.refresh_from_db()
        self.assertTrue(entry.quickbooks_id)

    def test_push_all_sends_bill_payments_and_erp_only_journal_entries(self):
        bill = self.bill()
        with push.suppress_auto_push():
            VendorPayment.objects.create(company=self.company, vendor=self.vendor, bill=bill, amount=Decimal("50"))
            entry = JournalEntry.objects.create(company=self.company, transaction_date=timezone.localdate(),
                                                description="Bank charge", source_module="cash_bank", status="draft")
            JournalEntryLine.objects.create(company=self.company, journal_entry=entry, line_number=1,
                                            account=Account.objects.get(company=self.company, code="6050"),
                                            debit=Decimal("5"), credit=Decimal("0"))
            JournalEntryLine.objects.create(company=self.company, journal_entry=entry, line_number=2,
                                            account=Account.objects.get(company=self.company, code="1010"),
                                            debit=Decimal("0"), credit=Decimal("5"))
            post_journal_entry(entry.id, self.admin, company=self.company)
        push.push_all(self.connection)
        self.assertTrue(VendorPayment.objects.get().quickbooks_id)
        entry.refresh_from_db()
        self.assertTrue(entry.quickbooks_id)

    # --- API for the Accounting > QuickBooks tab ---------------------------

    def test_overview_mapping_and_retry_endpoints(self):
        client = APIClient()
        client.force_authenticate(self.admin)

        res = client.get("/api/quickbooks/overview/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["connected"])
        self.assertIn("journal_entry", [row["entity_type"] for row in res.data["entities"]])

        res = client.post("/api/quickbooks/account-mappings/auto/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertGreater(res.data["mapped"], 0)

        rent = Account.objects.get(company=self.company, code="6050")
        res = client.post("/api/quickbooks/account-mappings/",
                          {"account": rent.id, "quickbooks_account_id": "3", "quickbooks_account_name": "Opening Balance Equity"},
                          format="json")
        row = next(r for r in res.data if r["code"] == "6050")
        self.assertEqual((row["quickbooks_account_id"], row["auto_mapped"]), ("3", False))

        self.fake.chart = [a for a in QB_CHART if a["AccountType"] != "Bank"]
        QuickBooksAccountMapping.objects.filter(account__code="1010").delete()
        self.manual_entry("6050", "1010")
        self.assertEqual(QuickBooksSyncError.objects.count(), 1)
        self.fake.chart = QB_CHART
        res = client.post("/api/quickbooks/retry-errors/")
        self.assertEqual(res.data["fixed"], 1)
        self.assertEqual(QuickBooksSyncError.objects.count(), 0)

        self.assertEqual(client.get("/api/quickbooks/accounts/").status_code, 200)
