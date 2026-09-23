from django.test import TestCase
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase
from rest_framework import status
from datetime import date
from accounts.models import Company, User
from .models import FiscalYear, AccountingPeriod, AccountType, Account, AccountingSettings
from .seeds import (
    ensure_account_types,
    seed_standard_chart_of_accounts,
    seed_standard_fiscal_year,
)


class AccountingModelTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Corp", slug="testcorp")
        self.types = ensure_account_types()

    def test_fiscal_year_validation(self):
        # Valid FY
        fy = FiscalYear.objects.create(
            company=self.company,
            name="FY 2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        self.assertEqual(str(fy), "FY 2026 (2026-01-01 to 2026-12-31) [Open]")

        # Invalid end date (start >= end)
        with self.assertRaises(ValidationError):
            FiscalYear.objects.create(
                company=self.company,
                name="FY Invalid",
                start_date=date(2026, 12, 31),
                end_date=date(2026, 1, 1),
            )

        # Overlapping FY
        with self.assertRaises(ValidationError):
            FiscalYear.objects.create(
                company=self.company,
                name="FY Overlap",
                start_date=date(2026, 6, 1),
                end_date=date(2027, 5, 31),
            )

    def test_accounting_period_validation(self):
        fy = FiscalYear.objects.create(
            company=self.company,
            name="FY 2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        # Period outside FY boundary
        with self.assertRaises(ValidationError):
            AccountingPeriod.objects.create(
                company=self.company,
                fiscal_year=fy,
                period_number=1,
                name="Late Period",
                start_date=date(2027, 1, 1),
                end_date=date(2027, 1, 31),
            )

    def test_account_hierarchy_validation(self):
        asset_type = self.types["Cash & Cash Equivalents"]

        parent = Account.objects.create(
            company=self.company,
            code="1000",
            name="Current Assets",
            account_type=asset_type,
        )

        child = Account.objects.create(
            company=self.company,
            code="1010",
            name="Bank Checking",
            account_type=asset_type,
            parent=parent,
        )

        self.assertEqual(child.parent, parent)
        self.assertTrue(parent.is_header)
        self.assertFalse(child.is_header)

        # Cannot be own parent
        parent.parent = parent
        with self.assertRaises(ValidationError):
            parent.clean()

        # Circular reference (parent -> child -> parent)
        parent.parent = None
        parent.save()
        parent.parent = child
        with self.assertRaises(ValidationError):
            parent.clean()

    def test_seeding_chart_of_accounts(self):
        created_count = seed_standard_chart_of_accounts(self.company)
        self.assertGreater(created_count, 15)

        # Check header and children
        current_assets = Account.objects.get(company=self.company, code="1000")
        self.assertTrue(current_assets.is_header)
        bank_acc = Account.objects.get(company=self.company, code="1010")
        self.assertEqual(bank_acc.parent, current_assets)

        # Check settings created
        settings = AccountingSettings.objects.get(company=self.company)
        self.assertIsNotNone(settings.retained_earnings_account)
        self.assertEqual(settings.retained_earnings_account.code, "3200")


class AccountingAPITests(TestCase):
    def setUp(self):
        self.company1 = Company.objects.create(name="Company One", slug="comp1")
        self.company2 = Company.objects.create(name="Company Two", slug="comp2")

        self.admin1 = User.objects.create_user(
            username="admin1",
            email="admin1@comp1.com",
            password="pass",
            role="admin",
            company=self.company1,
        )
        self.finance1 = User.objects.create_user(
            username="finance1",
            email="finance1@comp1.com",
            password="pass",
            role="finance",
            company=self.company1,
        )
        self.store_user = User.objects.create_user(
            username="store1",
            email="store1@comp1.com",
            password="pass",
            role="store",
            company=self.company1,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.admin1)

        # Seed standard types
        ensure_account_types()

    def test_account_types_list(self):
        res = self.client.get("/api/accounting/account-types/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreater(len(res.data), 10)

    def test_fiscal_year_crud_and_period_generation(self):
        # Create Fiscal Year
        payload = {
            "name": "FY 2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }
        res = self.client.post("/api/accounting/fiscal-years/", payload)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        fy_id = res.data["id"]

        # Generate periods
        gen_res = self.client.post(f"/api/accounting/fiscal-years/{fy_id}/generate_periods/")
        self.assertEqual(gen_res.status_code, status.HTTP_200_OK)
        self.assertEqual(gen_res.data["total_periods"], 12)

        # Query periods
        p_res = self.client.get(f"/api/accounting/periods/?fiscal_year={fy_id}")
        self.assertEqual(p_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(p_res.data), 12)

        # Lock first period
        first_p_id = p_res.data[0]["id"]
        lock_res = self.client.post(f"/api/accounting/periods/{first_p_id}/lock/")
        self.assertEqual(lock_res.status_code, status.HTTP_200_OK)
        first_p = AccountingPeriod.objects.get(id=first_p_id)
        self.assertEqual(first_p.status, "locked")

    def test_chart_of_accounts_tree_and_multi_tenancy(self):
        # Seed standard COA for Company 1
        res = self.client.post("/api/accounting/accounts/seed_standard_coa/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Tree endpoint
        tree_res = self.client.get("/api/accounting/accounts/tree/")
        self.assertEqual(tree_res.status_code, status.HTTP_200_OK)
        self.assertGreater(len(tree_res.data), 0)

        # Check first root account has children
        header_1000 = next((a for a in tree_res.data if a["code"] == "1000"), None)
        self.assertIsNotNone(header_1000)
        self.assertGreater(len(header_1000["children"]), 0)

        # Multi-tenancy check: User from Company 2 cannot see Company 1 accounts
        admin2 = User.objects.create_user(
            username="admin2",
            email="admin2@comp2.com",
            password="pass",
            role="admin",
            company=self.company2,
        )
        client2 = APIClient()
        client2.force_authenticate(user=admin2)
        comp2_accounts = client2.get("/api/accounting/accounts/")
        self.assertEqual(len(comp2_accounts.data), 0)

    def test_permission_denied_for_non_finance_role(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/accounts/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_foundation_summary_endpoint(self):
        # Seed both COA and FY
        self.client.post("/api/accounting/accounts/seed_standard_coa/")
        seed_standard_fiscal_year(self.company1, 2026)

        res = self.client.get("/api/accounting/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertGreater(res.data["total_accounts"], 0)
        self.assertIn("category_counts", res.data)
        self.assertIn("asset", res.data["category_counts"])
        self.assertTrue(res.data["setup_complete"])

    def test_erp_context_endpoint(self):
        from sales.models import Customer
        from procurement.models import Vendor
        from inventory.models import Item

        # Create sample operational entities
        Customer.objects.create(company=self.company1, name="Acme Beverages")
        Vendor.objects.create(company=self.company1, name="Global Packaging Co")
        Item.objects.create(company=self.company1, name="Glass Bottles 500ml", category="raw_material")

        res = self.client.get("/api/accounting/erp-context/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["company"]["id"], self.company1.id)
        self.assertEqual(res.data["counts"]["customers"], 1)
        self.assertEqual(res.data["counts"]["vendors"], 1)
        self.assertEqual(res.data["counts"]["items"], 1)
        self.assertIn("readiness", res.data)
        self.assertIn("integration_contract", res.data)
        self.assertGreater(len(res.data["integration_contract"]), 3)

        # Multi-tenancy check
        admin2 = User.objects.create_user(
            username="admin_comp2",
            email="admin@comp2.com",
            password="pass",
            role="admin",
            company=self.company2,
        )
        client2 = APIClient()
        client2.force_authenticate(user=admin2)
        res2 = client2.get("/api/accounting/erp-context/")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data["counts"]["customers"], 0)
        self.assertEqual(res2.data["counts"]["vendors"], 0)
        self.assertEqual(res2.data["counts"]["items"], 0)


from decimal import Decimal
from accounting.models import JournalEntry, JournalEntryLine


class DoubleEntryEngineTests(APITestCase):
    """
    Blueprint #8: Double-Entry Accounting Engine Tests.
    Tests mathematical equilibrium, period and lock date validation, atomic posting,
    reversal lineage, immutability, and tenant isolation.
    """

    def setUp(self):
        self.company1 = Company.objects.create(name="Apex Metals", slug="apex-metals")
        self.company2 = Company.objects.create(name="Beta Foundry", slug="beta-foundry")

        self.admin = User.objects.create_user(
            username="fin_admin",
            email="fin@apex.com",
            password="pass",
            role="admin",
            company=self.company1,
        )
        self.store_user = User.objects.create_user(
            username="unauth_user",
            email="store@apex.com",
            password="pass",
            role="store",
            company=self.company1,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

        # Seed types & accounts
        ensure_account_types()
        self.fy = FiscalYear.objects.create(
            company=self.company1,
            name="FY 2026",
            start_date="2026-01-01",
            end_date="2026-12-31"
        )
        # Create Period 1: Jan 2026
        self.period1 = AccountingPeriod.objects.create(
            company=self.company1,
            fiscal_year=self.fy,
            period_number=1,
            name="Jan 2026",
            start_date="2026-01-01",
            end_date="2026-01-31",
            status="open"
        )
        # Create Period 2: Feb 2026 (Locked)
        self.period2 = AccountingPeriod.objects.create(
            company=self.company1,
            fiscal_year=self.fy,
            period_number=2,
            name="Feb 2026",
            start_date="2026-02-01",
            end_date="2026-02-28",
            status="locked"
        )

        types = ensure_account_types()
        asset_type = types["Cash & Cash Equivalents"]
        revenue_type = types["Operating Sales Revenue"]
        expense_type = types["Cost of Goods Sold (Raw Materials)"]

        self.acc_bank = Account.objects.create(
            company=self.company1,
            code="1010",
            name="Operating Bank Account",
            account_type=asset_type
        )
        self.acc_sales = Account.objects.create(
            company=self.company1,
            code="4010",
            name="Product Sales Revenue",
            account_type=revenue_type
        )
        self.acc_cogs = Account.objects.create(
            company=self.company1,
            code="5010",
            name="Raw Material COGS",
            account_type=expense_type
        )

        # Company 2 Account
        self.acc_comp2 = Account.objects.create(
            company=self.company2,
            code="1010",
            name="Comp2 Bank",
            account_type=asset_type
        )

    def test_balanced_journal_entry_creates_and_posts_successfully(self):
        payload = {
            "transaction_date": "2026-01-15",
            "reference": "INV-1001",
            "description": "Customer cash payment for goods",
            "lines": [
                {"account": self.acc_bank.id, "debit": "1500.00", "credit": "0.00", "description": "Cash in bank"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "1500.00", "description": "Sales recognized"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        entry_id = res.data["id"]
        self.assertEqual(res.data["status"], "draft")
        self.assertTrue(res.data["is_balanced"])
        self.assertEqual(Decimal(res.data["total_debit"]), Decimal("1500.00"))
        self.assertEqual(Decimal(res.data["total_credit"]), Decimal("1500.00"))

        # Post entry
        post_res = self.client.post(f"/api/accounting/journal-entries/{entry_id}/post/")
        self.assertEqual(post_res.status_code, status.HTTP_200_OK)
        self.assertEqual(post_res.data["status"], "posted")
        self.assertIsNotNone(post_res.data["posted_at"])
        self.assertEqual(post_res.data["posted_by"], self.admin.id)

    def test_unbalanced_journal_entry_fails_posting(self):
        payload = {
            "transaction_date": "2026-01-15",
            "description": "Unbalanced entry attempt",
            "lines": [
                {"account": self.acc_bank.id, "debit": "1000.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "800.00"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        entry_id = res.data["id"]
        self.assertFalse(res.data["is_balanced"])

        # Attempt post -> Must fail
        post_res = self.client.post(f"/api/accounting/journal-entries/{entry_id}/post/")
        self.assertEqual(post_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unbalanced", str(post_res.data).lower())

    def test_line_with_both_debit_and_credit_rejected(self):
        payload = {
            "transaction_date": "2026-01-15",
            "lines": [
                {"account": self.acc_bank.id, "debit": "500.00", "credit": "500.00"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_modify_or_delete_posted_entry(self):
        payload = {
            "transaction_date": "2026-01-15",
            "lines": [
                {"account": self.acc_bank.id, "debit": "200.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "200.00"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        entry_id = res.data["id"]
        self.client.post(f"/api/accounting/journal-entries/{entry_id}/post/")

        # Attempt to edit
        patch_res = self.client.patch(f"/api/accounting/journal-entries/{entry_id}/", {"description": "Hacked"}, format="json")
        self.assertEqual(patch_res.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt to delete
        del_res = self.client.delete(f"/api/accounting/journal-entries/{entry_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_atomic_reversal_creates_exact_inverse_entry(self):
        payload = {
            "transaction_date": "2026-01-15",
            "reference": "ORIG-100",
            "description": "Original transaction",
            "lines": [
                {"account": self.acc_bank.id, "debit": "750.00", "credit": "0.00", "description": "Bank debit"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "750.00", "description": "Sales credit"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        entry_id = res.data["id"]
        self.client.post(f"/api/accounting/journal-entries/{entry_id}/post/")

        # Reverse entry
        rev_res = self.client.post(f"/api/accounting/journal-entries/{entry_id}/reverse/", {"reason": "Billing error"}, format="json")
        self.assertEqual(rev_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(rev_res.data["status"], "posted")
        self.assertEqual(rev_res.data["reversal_of"], entry_id)

        # Check original is now marked reversed
        orig_check = self.client.get(f"/api/accounting/journal-entries/{entry_id}/")
        self.assertEqual(orig_check.data["status"], "reversed")

        # Verify reversal lines are exactly flipped
        rev_lines = rev_res.data["lines"]
        bank_line = next(l for l in rev_lines if l["account"] == self.acc_bank.id)
        sales_line = next(l for l in rev_lines if l["account"] == self.acc_sales.id)
        self.assertEqual(Decimal(bank_line["credit"]), Decimal("750.00"))
        self.assertEqual(Decimal(bank_line["debit"]), Decimal("0.00"))
        self.assertEqual(Decimal(sales_line["debit"]), Decimal("750.00"))
        self.assertEqual(Decimal(sales_line["credit"]), Decimal("0.00"))

    def test_posting_blocked_on_locked_period(self):
        payload = {
            "transaction_date": "2026-02-15",  # Falls into Period 2 (Locked)
            "description": "Posting to locked period",
            "lines": [
                {"account": self.acc_bank.id, "debit": "300.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "300.00"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        entry_id = res.data["id"]
        post_res = self.client.post(f"/api/accounting/journal-entries/{entry_id}/post/")
        self.assertEqual(post_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("locked", str(post_res.data).lower())

    def test_posting_blocked_prior_to_lock_date(self):
        settings = AccountingSettings.objects.create(
            company=self.company1,
            default_currency="USD",
            lock_date="2026-01-20"
        )
        payload = {
            "transaction_date": "2026-01-10",  # Prior to lock date
            "description": "Retroactive entry attempt",
            "lines": [
                {"account": self.acc_bank.id, "debit": "100.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "100.00"},
            ]
        }
        res = self.client.post("/api/accounting/journal-entries/", payload, format="json")
        # Creating or posting prior to lock date must be rejected
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_cannot_create_or_post(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/journal-entries/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class GeneralLedgerTests(TestCase):
    """Test suite for Blueprint #9: General Ledger Layer."""

    def setUp(self):
        self.client = APIClient()
        self.company1 = Company.objects.create(name="Brewery Alpha", slug="brewery-alpha")
        self.company2 = Company.objects.create(name="Distillery Beta", slug="distillery-beta")

        self.admin = User.objects.create_user(
            username="finance_lead",
            email="finance@alpha.com",
            role="admin",
            company=self.company1
        )
        self.store_user = User.objects.create_user(
            username="storekeeper",
            email="store@alpha.com",
            role="store_manager",
            company=self.company1
        )
        self.client.force_authenticate(user=self.admin)

        # Ensure account types exist
        ensure_account_types()
        asset_type = AccountType.objects.get(name="Cash & Cash Equivalents")
        sales_type = AccountType.objects.get(name="Operating Sales Revenue")
        expense_type = AccountType.objects.get(name="Cost of Goods Sold (Raw Materials)")

        # Fiscal Year & Periods for Company 1
        self.fy = FiscalYear.objects.create(
            company=self.company1,
            name="FY 2026",
            start_date="2026-01-01",
            end_date="2026-12-31"
        )
        self.p1 = AccountingPeriod.objects.create(
            company=self.company1,
            fiscal_year=self.fy,
            period_number=1,
            name="Jan 2026",
            start_date="2026-01-01",
            end_date="2026-01-31",
            status="open"
        )
        self.p2 = AccountingPeriod.objects.create(
            company=self.company1,
            fiscal_year=self.fy,
            period_number=2,
            name="Feb 2026",
            start_date="2026-02-01",
            end_date="2026-02-28",
            status="open"
        )

        # Accounts
        self.acc_bank = Account.objects.create(
            company=self.company1,
            code="1010",
            name="Operating Bank Account",
            account_type=asset_type
        )
        self.acc_sales = Account.objects.create(
            company=self.company1,
            code="4010",
            name="Beer Sales",
            account_type=sales_type
        )
        self.acc_expense = Account.objects.create(
            company=self.company1,
            code="5010",
            name="Malt Expense",
            account_type=expense_type
        )

        # Company 2 Account (for isolation test)
        self.acc_comp2 = Account.objects.create(
            company=self.company2,
            code="1010",
            name="Comp2 Bank",
            account_type=asset_type
        )

    def test_posted_journal_entry_appears_in_account_ledger(self):
        # Create and post entry
        res = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-10",
            "reference": "SALE-01",
            "description": "Direct customer sale",
            "lines": [
                {"account": self.acc_bank.id, "debit": "500.00", "credit": "0.00", "description": "Cash receipt"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "500.00", "description": "Beer sale"},
            ]
        }, format="json")
        entry_id = res.data["id"]
        self.client.post(f"/api/accounting/journal-entries/{entry_id}/post/")

        # Query Bank ledger (Debit-normal asset)
        gl_bank = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_bank.id}")
        self.assertEqual(gl_bank.status_code, status.HTTP_200_OK)
        bank_data = gl_bank.data
        self.assertEqual(bank_data["summary"]["transaction_count"], 1)
        self.assertEqual(Decimal(bank_data["summary"]["closing_balance"]), Decimal("500.00"))
        self.assertEqual(bank_data["summary"]["closing_balance_side"], "DR")
        self.assertEqual(Decimal(bank_data["transactions"][0]["debit"]), Decimal("500.00"))
        self.assertEqual(Decimal(bank_data["transactions"][0]["running_balance"]), Decimal("500.00"))

        # Query Sales ledger (Credit-normal revenue)
        gl_sales = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_sales.id}")
        self.assertEqual(gl_sales.status_code, status.HTTP_200_OK)
        sales_data = gl_sales.data
        self.assertEqual(sales_data["summary"]["transaction_count"], 1)
        self.assertEqual(Decimal(sales_data["summary"]["closing_balance"]), Decimal("500.00"))
        self.assertEqual(sales_data["summary"]["closing_balance_side"], "CR")
        self.assertEqual(Decimal(sales_data["transactions"][0]["credit"]), Decimal("500.00"))
        self.assertEqual(Decimal(sales_data["transactions"][0]["running_balance"]), Decimal("500.00"))

    def test_draft_entry_does_not_appear_in_general_ledger(self):
        # Create draft entry without posting
        self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-10",
            "lines": [
                {"account": self.acc_bank.id, "debit": "999.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "999.00"},
            ]
        }, format="json")

        gl_res = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_bank.id}")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        self.assertEqual(gl_res.data["summary"]["transaction_count"], 0)
        self.assertEqual(Decimal(gl_res.data["summary"]["closing_balance"]), Decimal("0.00"))

    def test_running_balance_and_multiple_entries_aggregation(self):
        # Entry 1: Bank +1000, Sales +1000 (Jan 10)
        e1 = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-10",
            "lines": [
                {"account": self.acc_bank.id, "debit": "1000.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "1000.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e1}/post/")

        # Entry 2: Expense +400, Bank -400 (Jan 12)
        e2 = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-12",
            "lines": [
                {"account": self.acc_expense.id, "debit": "400.00", "credit": "0.00"},
                {"account": self.acc_bank.id, "debit": "0.00", "credit": "400.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e2}/post/")

        # Entry 3: Bank +200, Sales +200 (Jan 14)
        e3 = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-14",
            "lines": [
                {"account": self.acc_bank.id, "debit": "200.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "200.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e3}/post/")

        gl_bank = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_bank.id}")
        txs = gl_bank.data["transactions"]
        self.assertEqual(len(txs), 3)

        # Line 1: +1000 -> 1000.00
        self.assertEqual(Decimal(txs[0]["running_balance"]), Decimal("1000.00"))
        # Line 2: -400 -> 600.00
        self.assertEqual(Decimal(txs[1]["running_balance"]), Decimal("600.00"))
        # Line 3: +200 -> 800.00
        self.assertEqual(Decimal(txs[2]["running_balance"]), Decimal("800.00"))

        summary = gl_bank.data["summary"]
        self.assertEqual(Decimal(summary["period_total_debit"]), Decimal("1200.00"))
        self.assertEqual(Decimal(summary["period_total_credit"]), Decimal("400.00"))
        self.assertEqual(Decimal(summary["closing_balance"]), Decimal("800.00"))
        self.assertEqual(summary["closing_balance_side"], "DR")

    def test_opening_balance_calculation_with_date_filter(self):
        # Entry 1: Jan 10 (+1000)
        e1 = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-10",
            "lines": [
                {"account": self.acc_bank.id, "debit": "1000.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "1000.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e1}/post/")

        # Entry 2: Jan 12 (-400)
        e2 = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-12",
            "lines": [
                {"account": self.acc_expense.id, "debit": "400.00", "credit": "0.00"},
                {"account": self.acc_bank.id, "debit": "0.00", "credit": "400.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e2}/post/")

        # Entry 3: Jan 15 (+300)
        e3 = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-15",
            "lines": [
                {"account": self.acc_bank.id, "debit": "300.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "300.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e3}/post/")

        # Query with start_date="2026-01-14":
        # Prior balance = 1000 - 400 = 600.00 opening balance
        gl_res = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_bank.id}&start_date=2026-01-14")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        data = gl_res.data
        self.assertEqual(Decimal(data["summary"]["opening_balance"]), Decimal("600.00"))
        self.assertEqual(data["summary"]["opening_balance_side"], "DR")
        self.assertEqual(len(data["transactions"]), 1)
        self.assertEqual(Decimal(data["transactions"][0]["running_balance"]), Decimal("900.00"))
        self.assertEqual(Decimal(data["summary"]["closing_balance"]), Decimal("900.00"))

    def test_general_ledger_summary_endpoint(self):
        # Post a transaction
        e = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-10",
            "lines": [
                {"account": self.acc_bank.id, "debit": "250.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "250.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e}/post/")

        # Request summary
        res = self.client.get("/api/accounting/general-ledger/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data["totals"]["is_balanced"])
        self.assertEqual(Decimal(res.data["totals"]["grand_total_debit"]), Decimal("250.00"))
        self.assertEqual(Decimal(res.data["totals"]["grand_total_credit"]), Decimal("250.00"))

    def test_reversal_entry_nets_to_zero_in_ledger(self):
        # Post entry
        e = self.client.post("/api/accounting/journal-entries/", {
            "transaction_date": "2026-01-10",
            "lines": [
                {"account": self.acc_bank.id, "debit": "700.00", "credit": "0.00"},
                {"account": self.acc_sales.id, "debit": "0.00", "credit": "700.00"},
            ]
        }, format="json").data["id"]
        self.client.post(f"/api/accounting/journal-entries/{e}/post/")

        # Reverse entry
        self.client.post(f"/api/accounting/journal-entries/{e}/reverse/", {"reason": "Cancelled"}, format="json")

        # Bank ledger must have both original and reversal, ending at 0.00
        gl_bank = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_bank.id}")
        self.assertEqual(len(gl_bank.data["transactions"]), 2)
        self.assertEqual(Decimal(gl_bank.data["summary"]["closing_balance"]), Decimal("0.00"))

    def test_tenant_isolation_in_general_ledger(self):
        # Company 1 admin queries Company 2 account -> 400
        res = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_comp2.id}")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_denied_general_ledger(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/general-ledger/summary/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_empty_account_ledger_returns_clean_zero_state(self):
        gl_res = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_expense.id}")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        self.assertEqual(gl_res.data["summary"]["transaction_count"], 0)
        self.assertEqual(Decimal(gl_res.data["summary"]["opening_balance"]), Decimal("0.00"))
        self.assertEqual(Decimal(gl_res.data["summary"]["closing_balance"]), Decimal("0.00"))


from datetime import timedelta
from sales.models import Customer, Invoice, CustomerPayment
from accounting.receivables import (
    get_ar_summary,
    get_ar_aging_report,
    get_ar_invoices,
    post_invoice_to_ar,
    record_and_allocate_ar_payment,
    get_customer_ar_statement,
)


class AccountsReceivableTests(APITestCase):
    """
    Test suite for Master Accounting Blueprint Section #10: Accounts Receivable (AR).
    Covers customer receivables, invoice posting, payment allocation, aging engine,
    GL integration, tenant isolation, and period locking.
    """

    def setUp(self):
        # 1. Tenants
        self.comp1 = Company.objects.create(name="Brewing Co", slug="brewco")
        self.comp2 = Company.objects.create(name="Distillery Co", slug="distco")

        # 2. Users
        self.finance_user = User.objects.create_user(
            username="finuser",
            email="fin@brew.com",
            role="finance",
            company=self.comp1,
            password="pass"
        )
        self.store_user = User.objects.create_user(
            username="storeuser",
            email="store@brew.com",
            role="store_manager",
            company=self.comp1,
            password="pass"
        )
        self.comp2_user = User.objects.create_user(
            username="comp2fin",
            email="fin@dist.com",
            role="finance",
            company=self.comp2,
            password="pass"
        )

        # 3. Seed Accounts & Fiscal Year
        seed_standard_chart_of_accounts(self.comp1)
        seed_standard_fiscal_year(self.comp1, 2026)
        seed_standard_chart_of_accounts(self.comp2)
        seed_standard_fiscal_year(self.comp2, 2026)

        self.acc_ar = Account.objects.get(company=self.comp1, code="1100")
        self.acc_sales = Account.objects.get(company=self.comp1, code="4010")
        self.acc_bank = Account.objects.get(company=self.comp1, code="1010")

        # 4. Customers
        self.customer1 = Customer.objects.create(
            company=self.comp1,
            name="Acme Pubs",
            email="acme@pub.com",
            payment_terms="Net 30",
        )
        self.customer_comp2 = Customer.objects.create(
            company=self.comp2,
            name="Rival Bar",
            email="rival@bar.com",
        )

        self.client.force_authenticate(user=self.finance_user)

    def test_post_invoice_to_ar_creates_balanced_journal_entry(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 1, 15),
            due_date=date(2026, 2, 14),
            total_amount=Decimal("1500.00"),
            amount_paid=Decimal("0.00"),
            status="open",
        )

        res = self.client.post("/api/accounting/receivables/post-invoice/", {
            "invoice_id": inv.id
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        je_id = res.data["journal_entry_id"]

        je = JournalEntry.objects.get(id=je_id)
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.company, self.comp1)
        self.assertEqual(je.lines.count(), 2)

        # Verify Debit AR 1100, Credit Revenue 4010
        debit_line = je.lines.get(account=self.acc_ar)
        credit_line = je.lines.get(account=self.acc_sales)
        self.assertEqual(debit_line.debit, Decimal("1500.00"))
        self.assertEqual(debit_line.credit, Decimal("0.00"))
        self.assertEqual(credit_line.debit, Decimal("0.00"))
        self.assertEqual(credit_line.credit, Decimal("1500.00"))

        # Verify invoice list endpoint shows is_posted_to_gl=True
        inv_res = self.client.get("/api/accounting/receivables/invoices/")
        self.assertEqual(inv_res.status_code, status.HTTP_200_OK)
        item = [x for x in inv_res.data if x["id"] == inv.id][0]
        self.assertTrue(item["is_posted_to_gl"])
        self.assertEqual(item["journal_entry_id"], je.id)

    def test_posted_ar_invoice_flows_into_general_ledger(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 1, 15),
            due_date=date(2026, 2, 14),
            total_amount=Decimal("2000.00"),
            amount_paid=Decimal("0.00"),
            status="open",
        )
        self.client.post("/api/accounting/receivables/post-invoice/", {"invoice_id": inv.id}, format="json")

        # Check Account 1100 (AR) in General Ledger
        gl_ar = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ar.id}")
        self.assertEqual(gl_ar.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(gl_ar.data["summary"]["period_total_debit"]), Decimal("2000.00"))
        self.assertEqual(Decimal(gl_ar.data["summary"]["closing_balance"]), Decimal("2000.00"))
        self.assertEqual(gl_ar.data["summary"]["closing_balance_side"], "DR")

        # Check Account 4010 (Sales Revenue) in General Ledger
        gl_sales = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_sales.id}")
        self.assertEqual(gl_sales.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(gl_sales.data["summary"]["period_total_credit"]), Decimal("2000.00"))
        self.assertEqual(Decimal(gl_sales.data["summary"]["closing_balance"]), Decimal("2000.00"))
        self.assertEqual(gl_sales.data["summary"]["closing_balance_side"], "CR")

    def test_duplicate_invoice_posting_is_prevented(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 1, 15),
            total_amount=Decimal("500.00"),
            status="open",
        )
        # First post succeeds
        res1 = self.client.post("/api/accounting/receivables/post-invoice/", {"invoice_id": inv.id}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Second post is rejected with duplicate error
        res2 = self.client.post("/api/accounting/receivables/post-invoice/", {"invoice_id": inv.id}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been posted", str(res2.data))

    def test_partial_and_full_payment_allocation(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 1, 10),
            due_date=date(2026, 2, 10),
            total_amount=Decimal("1000.00"),
            amount_paid=Decimal("0.00"),
            status="open",
        )
        self.client.post("/api/accounting/receivables/post-invoice/", {"invoice_id": inv.id}, format="json")

        # 1. Partial payment: $400
        res1 = self.client.post("/api/accounting/receivables/record-payment/", {
            "customer_id": self.customer1.id,
            "amount": "400.00",
            "payment_date": "2026-01-20",
            "method": "bank_transfer",
            "reference": "WIRE-400",
            "allocations": [{"invoice_id": inv.id, "amount": "400.00"}]
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        inv.refresh_from_db()
        self.assertEqual(inv.status, "partial")
        self.assertEqual(inv.amount_paid, Decimal("400.00"))
        self.assertEqual(inv.balance_due, Decimal("600.00"))

        # Verify Bank GL increased by $400, AR balance is now $600
        gl_bank = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_bank.id}")
        self.assertEqual(Decimal(gl_bank.data["summary"]["closing_balance"]), Decimal("400.00"))

        gl_ar = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ar.id}")
        self.assertEqual(Decimal(gl_ar.data["summary"]["closing_balance"]), Decimal("600.00"))

        # 2. Full remaining payment: $600
        res2 = self.client.post("/api/accounting/receivables/record-payment/", {
            "customer_id": self.customer1.id,
            "amount": "600.00",
            "payment_date": "2026-01-25",
            "method": "bank_transfer",
            "reference": "WIRE-600",
            "allocations": [{"invoice_id": inv.id, "amount": "600.00"}]
        }, format="json")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

        inv.refresh_from_db()
        self.assertEqual(inv.status, "paid")
        self.assertEqual(inv.balance_due, Decimal("0.00"))

        # AR GL is fully settled (closing balance 0.00)
        gl_ar2 = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ar.id}")
        self.assertEqual(Decimal(gl_ar2.data["summary"]["closing_balance"]), Decimal("0.00"))

    def test_multiple_invoice_payment_allocation(self):
        inv1 = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 1, 5),
            total_amount=Decimal("300.00"),
            status="open",
        )
        inv2 = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 1, 8),
            total_amount=Decimal("700.00"),
            status="open",
        )
        self.client.post("/api/accounting/receivables/post-invoice/", {"invoice_id": inv1.id}, format="json")
        self.client.post("/api/accounting/receivables/post-invoice/", {"invoice_id": inv2.id}, format="json")

        # Allocate $1000 payment across both invoices
        res = self.client.post("/api/accounting/receivables/record-payment/", {
            "customer_id": self.customer1.id,
            "amount": "1000.00",
            "payment_date": "2026-01-22",
            "method": "bank_transfer",
            "reference": "BULK-1000",
            "allocations": [
                {"invoice_id": inv1.id, "amount": "300.00"},
                {"invoice_id": inv2.id, "amount": "700.00"},
            ]
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        inv1.refresh_from_db()
        inv2.refresh_from_db()
        self.assertEqual(inv1.status, "paid")
        self.assertEqual(inv2.status, "paid")

    def test_ar_aging_buckets_calculation(self):
        ref_date = date(2026, 4, 15)

        # 1. Current (due in future)
        Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 4, 1), due_date=date(2026, 4, 20),
            total_amount=Decimal("100.00"), status="open",
        )
        # 2. 1-30 days overdue (due 15 days ago, April 01)
        Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 3, 1), due_date=date(2026, 4, 1),
            total_amount=Decimal("200.00"), status="open",
        )
        # 3. 31-60 days overdue (due 45 days ago, March 01)
        Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 2, 1), due_date=date(2026, 3, 1),
            total_amount=Decimal("300.00"), status="open",
        )
        # 4. 61-90 days overdue (due 75 days ago, Jan 30)
        Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 1, 1), due_date=date(2026, 1, 30),
            total_amount=Decimal("400.00"), status="open",
        )
        # 5. 90+ days overdue (due 105 days ago, Dec 31 2025)
        Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2025, 12, 1), due_date=date(2025, 12, 31),
            total_amount=Decimal("500.00"), status="open",
        )

        res = self.client.get(f"/api/accounting/receivables/aging/?as_of_date={ref_date.isoformat()}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        totals = res.data["totals"]

        self.assertEqual(Decimal(totals["total_receivables"]), Decimal("1500.00"))
        self.assertEqual(Decimal(totals["current"]), Decimal("100.00"))
        self.assertEqual(Decimal(totals["days_1_30"]), Decimal("200.00"))
        self.assertEqual(Decimal(totals["days_31_60"]), Decimal("300.00"))
        self.assertEqual(Decimal(totals["days_61_90"]), Decimal("400.00"))
        self.assertEqual(Decimal(totals["days_90_plus"]), Decimal("500.00"))
        self.assertEqual(Decimal(totals["overdue_total"]), Decimal("1400.00"))

    def test_customer_ar_statement(self):
        inv = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 1, 10), due_date=date(2026, 1, 25),
            total_amount=Decimal("800.00"), status="open",
        )
        CustomerPayment.objects.create(
            company=self.comp1, customer=self.customer1, invoice=inv,
            amount=Decimal("300.00"), payment_date=date(2026, 1, 15),
            method="cheque", reference="CHK-1234",
        )

        res = self.client.get(f"/api/accounting/receivables/customer/{self.customer1.id}/statement/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["transactions"]), 2)
        # Line 1: Invoice debit 800, running balance 800
        self.assertEqual(Decimal(res.data["transactions"][0]["debit"]), Decimal("800.00"))
        self.assertEqual(Decimal(res.data["transactions"][0]["running_balance"]), Decimal("800.00"))
        # Line 2: Payment credit 300, running balance 500
        self.assertEqual(Decimal(res.data["transactions"][1]["credit"]), Decimal("300.00"))
        self.assertEqual(Decimal(res.data["transactions"][1]["running_balance"]), Decimal("500.00"))
        self.assertEqual(Decimal(res.data["summary"]["ending_balance"]), Decimal("500.00"))

    def test_locked_period_blocks_invoice_and_payment_posting(self):
        # Lock Period 1 (Jan 2026)
        p1 = AccountingPeriod.objects.get(company=self.comp1, period_number=1)
        p1.status = "locked"
        p1.save()

        inv = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 1, 15), total_amount=Decimal("350.00"),
            status="open",
        )

        # Attempt to post in locked period
        res = self.client.post("/api/accounting/receivables/post-invoice/", {
            "invoice_id": inv.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("locked", str(res.data).lower())

    def test_tenant_isolation_in_receivables(self):
        # Company 2 invoice
        inv_comp2 = Invoice.objects.create(
            company=self.comp2, customer=self.customer_comp2,
            invoice_date=date(2026, 1, 15), total_amount=Decimal("999.00"),
            status="open",
        )

        # Company 1 user attempts to post Company 2 invoice -> 400
        res = self.client.post("/api/accounting/receivables/post-invoice/", {
            "invoice_id": inv_comp2.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Company 1 user attempts to query Company 2 customer statement -> 400
        res2 = self.client.get(f"/api/accounting/receivables/customer/{self.customer_comp2.id}/statement/")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_denied_ar_access(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/receivables/summary/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_payment_amounts_rejected(self):
        inv = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 1, 10), total_amount=Decimal("100.00"),
            status="open",
        )

        # Negative amount
        res1 = self.client.post("/api/accounting/receivables/record-payment/", {
            "customer_id": self.customer1.id,
            "amount": "-50.00",
            "allocations": [{"invoice_id": inv.id, "amount": "-50.00"}]
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)

        # Amount exceeding balance due
        res2 = self.client.post("/api/accounting/receivables/record-payment/", {
            "customer_id": self.customer1.id,
            "amount": "250.00",
            "allocations": [{"invoice_id": inv.id, "amount": "250.00"}]
        }, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exceeds balance due", str(res2.data).lower())




