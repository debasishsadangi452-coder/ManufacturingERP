from django.test import TestCase
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase
from rest_framework import status
from datetime import date
from django.db.models import Sum
from accounts.models import Company, User
from .models import FiscalYear, AccountingPeriod, AccountType, Account, AccountingSettings
from .seeds import (
    ensure_account_types,
    seed_standard_chart_of_accounts,
    seed_standard_fiscal_year,
)
from production.models import Recipe, RecipeIngredient, ProductionOrder


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


from procurement.models import Vendor, Bill, VendorPayment


class AccountsPayableTests(APITestCase):
    """
    Test suite for Master Accounting Blueprint Section #11: Accounts Payable (AP).
    Covers vendor payables, bill posting, payment allocation, aging engine,
    GL integration, tenant isolation, and period locking.
    """

    def setUp(self):
        # 1. Tenants
        self.comp1 = Company.objects.create(name="Brewing Co", slug="brewco")
        self.comp2 = Company.objects.create(name="Distillery Co", slug="distco")

        # 2. Users
        self.finance_user = User.objects.create_user(
            username="finuser_ap",
            email="fin_ap@brew.com",
            role="finance",
            company=self.comp1,
            password="pass"
        )
        self.store_user = User.objects.create_user(
            username="storeuser_ap",
            email="store_ap@brew.com",
            role="store_manager",
            company=self.comp1,
            password="pass"
        )
        self.comp2_user = User.objects.create_user(
            username="comp2fin_ap",
            email="fin_ap@dist.com",
            role="finance",
            company=self.comp2,
            password="pass"
        )

        # 3. Seed Accounts & Fiscal Year
        seed_standard_chart_of_accounts(self.comp1)
        seed_standard_fiscal_year(self.comp1, 2026)
        seed_standard_chart_of_accounts(self.comp2)
        seed_standard_fiscal_year(self.comp2, 2026)

        self.acc_ap = Account.objects.get(company=self.comp1, code="2010")
        self.acc_raw_inv = Account.objects.get(company=self.comp1, code="1210")
        self.acc_bank = Account.objects.get(company=self.comp1, code="1010")

        # 4. Vendors
        self.vendor1 = Vendor.objects.create(
            company=self.comp1,
            name="Hop Supplier Co",
            email="hops@supplier.com",
            payment_terms="Net 30",
        )
        self.vendor_comp2 = Vendor.objects.create(
            company=self.comp2,
            name="Rival Grain Co",
            email="grain@rival.com",
        )

        self.client.force_authenticate(user=self.finance_user)

    def test_post_bill_to_ap_creates_balanced_journal_entry(self):
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-HOP-101",
            bill_date=date(2026, 1, 15),
            due_date=date(2026, 2, 14),
            total_amount=Decimal("1200.00"),
            amount_paid=Decimal("0.00"),
            status="open",
        )

        res = self.client.post("/api/accounting/payables/post-bill/", {
            "bill_id": bill.id
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        je_id = res.data["journal_entry_id"]

        je = JournalEntry.objects.get(id=je_id)
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.company, self.comp1)
        self.assertEqual(je.lines.count(), 2)

        # Verify Debit Raw Materials Inventory 1210, Credit Accounts Payable 2010
        debit_line = je.lines.get(account=self.acc_raw_inv)
        credit_line = je.lines.get(account=self.acc_ap)
        self.assertEqual(debit_line.debit, Decimal("1200.00"))
        self.assertEqual(debit_line.credit, Decimal("0.00"))
        self.assertEqual(credit_line.debit, Decimal("0.00"))
        self.assertEqual(credit_line.credit, Decimal("1200.00"))

        # Verify bill list endpoint shows is_posted_to_gl=True
        bills_res = self.client.get("/api/accounting/payables/bills/")
        self.assertEqual(bills_res.status_code, status.HTTP_200_OK)
        item = [x for x in bills_res.data if x["id"] == bill.id][0]
        self.assertTrue(item["is_posted_to_gl"])
        self.assertEqual(item["journal_entry_id"], je.id)

    def test_posted_ap_bill_flows_into_general_ledger(self):
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-HOP-102",
            bill_date=date(2026, 1, 15),
            due_date=date(2026, 2, 14),
            total_amount=Decimal("2500.00"),
            amount_paid=Decimal("0.00"),
            status="open",
        )
        self.client.post("/api/accounting/payables/post-bill/", {"bill_id": bill.id}, format="json")

        # Check Account 2010 (AP) in General Ledger (Normal balance is Credit)
        gl_ap = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ap.id}")
        self.assertEqual(gl_ap.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(gl_ap.data["summary"]["period_total_credit"]), Decimal("2500.00"))
        self.assertEqual(Decimal(gl_ap.data["summary"]["closing_balance"]), Decimal("2500.00"))
        self.assertEqual(gl_ap.data["summary"]["closing_balance_side"], "CR")

        # Check Account 1210 (Raw Materials Inventory) in General Ledger
        gl_inv = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_raw_inv.id}")
        self.assertEqual(gl_inv.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(gl_inv.data["summary"]["period_total_debit"]), Decimal("2500.00"))
        self.assertEqual(Decimal(gl_inv.data["summary"]["closing_balance"]), Decimal("2500.00"))
        self.assertEqual(gl_inv.data["summary"]["closing_balance_side"], "DR")

    def test_draft_bill_does_not_affect_gl(self):
        Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-DRAFT-1",
            bill_date=date(2026, 1, 15),
            total_amount=Decimal("3000.00"),
            status="open",
        )
        # Verify GL has zero balance before posting
        gl_ap = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ap.id}")
        self.assertEqual(gl_ap.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(gl_ap.data["summary"]["period_total_credit"]), Decimal("0.00"))
        self.assertEqual(Decimal(gl_ap.data["summary"]["closing_balance"]), Decimal("0.00"))

    def test_duplicate_bill_posting_is_prevented(self):
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-HOP-103",
            bill_date=date(2026, 1, 15),
            total_amount=Decimal("500.00"),
            status="open",
        )
        # First post succeeds
        res1 = self.client.post("/api/accounting/payables/post-bill/", {"bill_id": bill.id}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        # Second post is rejected with duplicate error
        res2 = self.client.post("/api/accounting/payables/post-bill/", {"bill_id": bill.id}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been posted", str(res2.data))

    def test_partial_and_full_vendor_payment_allocation(self):
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-HOP-104",
            bill_date=date(2026, 1, 10),
            due_date=date(2026, 2, 10),
            total_amount=Decimal("1000.00"),
            amount_paid=Decimal("0.00"),
            status="open",
        )
        self.client.post("/api/accounting/payables/post-bill/", {"bill_id": bill.id}, format="json")

        # 1. Partial payment: $400
        res1 = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "400.00",
            "payment_date": "2026-01-20",
            "method": "bank_transfer",
            "reference": "VWIRE-400",
            "allocations": [{"bill_id": bill.id, "amount": "400.00"}]
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        bill.refresh_from_db()
        self.assertEqual(bill.status, "partial")
        self.assertEqual(bill.amount_paid, Decimal("400.00"))
        self.assertEqual(bill.balance_due, Decimal("600.00"))

        # Verify AP GL closing credit balance is now $600
        gl_ap = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ap.id}")
        self.assertEqual(Decimal(gl_ap.data["summary"]["closing_balance"]), Decimal("600.00"))

        # 2. Full remaining payment: $600
        res2 = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "600.00",
            "payment_date": "2026-01-25",
            "method": "bank_transfer",
            "reference": "VWIRE-600",
            "allocations": [{"bill_id": bill.id, "amount": "600.00"}]
        }, format="json")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

        bill.refresh_from_db()
        self.assertEqual(bill.status, "paid")
        self.assertEqual(bill.balance_due, Decimal("0.00"))

        # AP GL is fully settled (closing balance 0.00)
        gl_ap2 = self.client.get(f"/api/accounting/general-ledger/?account={self.acc_ap.id}")
        self.assertEqual(Decimal(gl_ap2.data["summary"]["closing_balance"]), Decimal("0.00"))

    def test_multiple_bill_payment_allocation(self):
        bill1 = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-HOP-105A",
            bill_date=date(2026, 1, 5),
            total_amount=Decimal("350.00"),
            status="open",
        )
        bill2 = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-HOP-105B",
            bill_date=date(2026, 1, 8),
            total_amount=Decimal("650.00"),
            status="open",
        )
        self.client.post("/api/accounting/payables/post-bill/", {"bill_id": bill1.id}, format="json")
        self.client.post("/api/accounting/payables/post-bill/", {"bill_id": bill2.id}, format="json")

        # Allocate $1000 payment across both bills
        res = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "1000.00",
            "payment_date": "2026-01-22",
            "method": "bank_transfer",
            "reference": "VBULK-1000",
            "allocations": [
                {"bill_id": bill1.id, "amount": "350.00"},
                {"bill_id": bill2.id, "amount": "650.00"},
            ]
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        bill1.refresh_from_db()
        bill2.refresh_from_db()
        self.assertEqual(bill1.status, "paid")
        self.assertEqual(bill2.status, "paid")

    def test_ap_aging_buckets_calculation(self):
        ref_date = date(2026, 4, 15)

        # 1. Current (due in future)
        Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="B-CURR",
            bill_date=date(2026, 4, 1), due_date=date(2026, 4, 20),
            total_amount=Decimal("100.00"), status="open",
        )
        # 2. 1-30 days overdue (due 14 days ago, April 01)
        Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="B-1-30",
            bill_date=date(2026, 3, 1), due_date=date(2026, 4, 1),
            total_amount=Decimal("200.00"), status="open",
        )
        # 3. 31-60 days overdue (due 45 days ago, March 01)
        Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="B-31-60",
            bill_date=date(2026, 2, 1), due_date=date(2026, 3, 1),
            total_amount=Decimal("300.00"), status="open",
        )
        # 4. 61-90 days overdue (due 75 days ago, Jan 30)
        Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="B-61-90",
            bill_date=date(2026, 1, 1), due_date=date(2026, 1, 30),
            total_amount=Decimal("400.00"), status="open",
        )
        # 5. 90+ days overdue (due 105 days ago, Dec 31 2025)
        Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="B-90+",
            bill_date=date(2025, 12, 1), due_date=date(2025, 12, 31),
            total_amount=Decimal("500.00"), status="open",
        )

        res = self.client.get(f"/api/accounting/payables/aging/?as_of_date={ref_date.isoformat()}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        totals = res.data["totals"]

        self.assertEqual(Decimal(totals["total_payables"]), Decimal("1500.00"))
        self.assertEqual(Decimal(totals["current"]), Decimal("100.00"))
        self.assertEqual(Decimal(totals["days_1_30"]), Decimal("200.00"))
        self.assertEqual(Decimal(totals["days_31_60"]), Decimal("300.00"))
        self.assertEqual(Decimal(totals["days_61_90"]), Decimal("400.00"))
        self.assertEqual(Decimal(totals["days_90_plus"]), Decimal("500.00"))
        self.assertEqual(Decimal(totals["overdue_total"]), Decimal("1400.00"))

    def test_vendor_ap_statement(self):
        bill = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-STM-1",
            bill_date=date(2026, 1, 10), due_date=date(2026, 1, 25),
            total_amount=Decimal("1000.00"), status="open",
        )
        VendorPayment.objects.create(
            company=self.comp1, vendor=self.vendor1, bill=bill,
            amount=Decimal("400.00"), payment_date=date(2026, 1, 15),
            method="bank_transfer", reference="VPMT-1234",
        )

        res = self.client.get(f"/api/accounting/payables/vendor/{self.vendor1.id}/statement/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["transactions"]), 2)
        # Line 1: Bill credit 1000, running balance 1000
        self.assertEqual(Decimal(res.data["transactions"][0]["credit"]), Decimal("1000.00"))
        self.assertEqual(Decimal(res.data["transactions"][0]["running_balance"]), Decimal("1000.00"))
        # Line 2: Payment debit 400, running balance 600
        self.assertEqual(Decimal(res.data["transactions"][1]["debit"]), Decimal("400.00"))
        self.assertEqual(Decimal(res.data["transactions"][1]["running_balance"]), Decimal("600.00"))
        self.assertEqual(Decimal(res.data["summary"]["ending_balance"]), Decimal("600.00"))

    def test_locked_period_blocks_bill_and_payment_posting(self):
        # Lock Period 1 (Jan 2026)
        p1 = AccountingPeriod.objects.get(company=self.comp1, period_number=1)
        p1.status = "locked"
        p1.save()

        bill = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-LOCK-1",
            bill_date=date(2026, 1, 15), total_amount=Decimal("450.00"),
            status="open",
        )

        # Attempt to post in locked period
        res = self.client.post("/api/accounting/payables/post-bill/", {
            "bill_id": bill.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("locked", str(res.data).lower())

    def test_tenant_isolation_in_payables(self):
        # Company 2 bill
        bill_comp2 = Bill.objects.create(
            company=self.comp2, vendor=self.vendor_comp2,
            bill_number="BILL-COMP2-1",
            bill_date=date(2026, 1, 15), total_amount=Decimal("888.00"),
            status="open",
        )

        # Company 1 user attempts to post Company 2 bill -> 400
        res = self.client.post("/api/accounting/payables/post-bill/", {
            "bill_id": bill_comp2.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Company 1 user attempts to query Company 2 vendor statement -> 400
        res2 = self.client.get(f"/api/accounting/payables/vendor/{self.vendor_comp2.id}/statement/")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_user_denied_ap_access(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/payables/summary/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_payment_amounts_rejected(self):
        bill = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-VAL-1",
            bill_date=date(2026, 1, 10), total_amount=Decimal("150.00"),
            status="open",
        )

        # Negative amount
        res1 = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "-50.00",
            "allocations": [{"bill_id": bill.id, "amount": "-50.00"}]
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)

        # Zero amount
        res1b = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "0.00",
            "allocations": [{"bill_id": bill.id, "amount": "0.00"}]
        }, format="json")
        self.assertEqual(res1b.status_code, status.HTTP_400_BAD_REQUEST)

        # Amount exceeding balance due
        res2 = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "250.00",
            "allocations": [{"bill_id": bill.id, "amount": "250.00"}]
        }, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exceeds balance due", str(res2.data).lower())

        # Invalid vendor/bill relationship
        res3 = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor_comp2.id,
            "amount": "50.00",
            "allocations": [{"bill_id": bill.id, "amount": "50.00"}]
        }, format="json")
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)


class SalesAccountingTests(APITestCase):
    """
    Test suite for Master Accounting Blueprint Section #12: Sales-to-Accounting.
    Covers operational sales invoice posting, preview calculation, revenue recognition,
    tax liability posting, reversal/cancellation, GL integration, and security controls.
    """

    def setUp(self):
        # 1. Tenants
        self.comp1 = Company.objects.create(name="Brewing Master Co", slug="brewmaster")
        self.comp2 = Company.objects.create(name="Competitor Distilling", slug="compdist")

        # 2. Users
        self.finance_user = User.objects.create_user(
            username="salesfin",
            email="fin@salesmaster.com",
            role="finance",
            company=self.comp1,
            password="pass"
        )
        self.store_user = User.objects.create_user(
            username="salesstore",
            email="store@salesmaster.com",
            role="store_manager",
            company=self.comp1,
            password="pass"
        )
        self.comp2_user = User.objects.create_user(
            username="comp2salesfin",
            email="fin@compdist.com",
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
            name="The Hoppy Tavern",
            email="hoppy@tavern.com",
            payment_terms="Net 30",
        )
        self.customer_comp2 = Customer.objects.create(
            company=self.comp2,
            name="Other Tavern",
            email="other@tavern.com",
        )

        self.client.force_authenticate(user=self.finance_user)

    def test_sales_accounting_preview_endpoint(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 10),
            due_date=date(2026, 3, 12),
            total_amount=Decimal("1200.00"),
            status="open",
        )

        # Preview with tax
        res = self.client.post(f"/api/accounting/sales/{inv.id}/preview/", {
            "tax_amount": "120.00"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data
        self.assertTrue(data["is_balanced"])
        self.assertEqual(data["total_debit"], 1200.00)
        self.assertEqual(data["total_credit"], 1200.00)
        self.assertEqual(data["net_sales"], 1080.00)
        self.assertEqual(data["tax_amount"], 120.00)
        self.assertEqual(len(data["lines"]), 3)

    def test_post_sales_invoice_creates_balanced_journal_entry(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 10),
            total_amount=Decimal("1500.00"),
            status="open",
        )

        res = self.client.post(f"/api/accounting/sales/{inv.id}/post/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIn("journal_entry_id", res.data)

        je = JournalEntry.objects.get(pk=res.data["journal_entry_id"])
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.total_debit, Decimal("1500.00"))
        self.assertEqual(je.total_credit, Decimal("1500.00"))

        # Verify lines
        lines = list(je.lines.order_by("line_number"))
        self.assertEqual(len(lines), 2)
        # Line 1: AR Debit
        self.assertEqual(lines[0].account.code, "1100")
        self.assertEqual(lines[0].debit, Decimal("1500.00"))
        self.assertEqual(lines[0].credit, Decimal("0.00"))
        # Line 2: Revenue Credit
        self.assertEqual(lines[1].account.code, "4010")
        self.assertEqual(lines[1].debit, Decimal("0.00"))
        self.assertEqual(lines[1].credit, Decimal("1500.00"))

        # Customer balance synced
        self.customer1.refresh_from_db()
        self.assertEqual(self.customer1.balance_due, Decimal("1500.00"))

    def test_post_sales_invoice_with_tax(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 15),
            total_amount=Decimal("1100.00"),
            status="open",
        )

        res = self.client.post(f"/api/accounting/sales/{inv.id}/post/", {
            "tax_amount": "100.00"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        je = JournalEntry.objects.get(pk=res.data["journal_entry_id"])
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.total_debit, Decimal("1100.00"))
        self.assertEqual(je.total_credit, Decimal("1100.00"))

        lines = list(je.lines.order_by("line_number"))
        self.assertEqual(len(lines), 3)

        # Line 1: AR Debit = 1100
        self.assertEqual(lines[0].account.code, "1100")
        self.assertEqual(lines[0].debit, Decimal("1100.00"))
        self.assertEqual(lines[0].credit, Decimal("0.00"))

        # Line 2: Revenue Credit = 1000
        self.assertEqual(lines[1].account.code, "4010")
        self.assertEqual(lines[1].debit, Decimal("0.00"))
        self.assertEqual(lines[1].credit, Decimal("1000.00"))

        # Line 3: Tax Payable Credit = 100
        self.assertEqual(lines[2].account.code, "2200")
        self.assertEqual(lines[2].debit, Decimal("0.00"))
        self.assertEqual(lines[2].credit, Decimal("100.00"))

    def test_duplicate_sales_invoice_posting_is_prevented(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 12),
            total_amount=Decimal("750.00"),
            status="open",
        )

        res1 = self.client.post(f"/api/accounting/sales/{inv.id}/post/", {}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        res2 = self.client.post(f"/api/accounting/sales/{inv.id}/post/", {}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been posted", str(res2.data).lower())

    def test_posted_sales_invoice_flows_into_general_ledger(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 18),
            total_amount=Decimal("2000.00"),
            status="open",
        )

        self.client.post(f"/api/accounting/sales/{inv.id}/post/", {
            "tax_amount": "200.00"
        }, format="json")

        # 1. GL Summary
        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        balances = {item["code"]: item for item in gl_res.data["accounts"]}

        # AR (1100): Debit 2000, closing balance 2000
        self.assertEqual(Decimal(balances["1100"]["period_debit"]), Decimal("2000.00"))
        self.assertEqual(Decimal(balances["1100"]["closing_balance"]), Decimal("2000.00"))

        # Sales Revenue (4010): Credit 1800, closing balance 1800
        self.assertEqual(Decimal(balances["4010"]["period_credit"]), Decimal("1800.00"))
        self.assertEqual(Decimal(balances["4010"]["closing_balance"]), Decimal("1800.00"))

        # Sales Tax Payable (2200): Credit 200, closing balance 200
        self.assertEqual(Decimal(balances["2200"]["period_credit"]), Decimal("200.00"))
        self.assertEqual(Decimal(balances["2200"]["closing_balance"]), Decimal("200.00"))

    def test_draft_or_unposted_sales_invoice_does_not_affect_gl(self):
        Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 20),
            total_amount=Decimal("3500.00"),
            status="open",
        )

        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        balances = {item["code"]: item for item in gl_res.data["accounts"]}
        self.assertEqual(Decimal(balances["1100"]["closing_balance"]), Decimal("0.00"))
        self.assertEqual(Decimal(balances["4010"]["closing_balance"]), Decimal("0.00"))

    def test_reverse_sales_invoice_accounting(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 22),
            total_amount=Decimal("800.00"),
            status="open",
        )

        post_res = self.client.post(f"/api/accounting/sales/{inv.id}/post/", {}, format="json")
        self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)

        # Now reverse
        rev_res = self.client.post(f"/api/accounting/sales/{inv.id}/reverse/", {
            "reason": "Customer cancellation / order returned"
        }, format="json")
        self.assertEqual(rev_res.status_code, status.HTTP_200_OK)
        self.assertEqual(rev_res.data["status"], "cancelled")

        inv.refresh_from_db()
        self.assertEqual(inv.status, "cancelled")

        # Check reversing journal entry
        reversal_je_id = rev_res.data["reversal_journal_entry_id"]
        reversal_je = JournalEntry.objects.get(pk=reversal_je_id)
        self.assertEqual(reversal_je.status, "posted")
        self.assertEqual(reversal_je.total_debit, Decimal("800.00"))
        self.assertEqual(reversal_je.total_credit, Decimal("800.00"))

        # GL Net Balance for AR and Sales should now be 0
        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        balances = {item["code"]: item for item in gl_res.data["accounts"]}
        self.assertEqual(Decimal(balances["1100"]["closing_balance"]), Decimal("0.00"))
        self.assertEqual(Decimal(balances["4010"]["closing_balance"]), Decimal("0.00"))

        # Customer balance synced back to 0
        self.customer1.refresh_from_db()
        self.assertEqual(self.customer1.balance_due, Decimal("0.00"))

    def test_reversal_blocked_if_payments_already_applied(self):
        inv = Invoice.objects.create(
            company=self.comp1,
            customer=self.customer1,
            invoice_date=date(2026, 2, 25),
            total_amount=Decimal("1000.00"),
            status="open",
        )
        self.client.post(f"/api/accounting/sales/{inv.id}/post/", {}, format="json")

        # Apply a payment
        inv.apply_payment(Decimal("300.00"))

        rev_res = self.client.post(f"/api/accounting/sales/{inv.id}/reverse/", {
            "reason": "Invalid attempt"
        }, format="json")
        self.assertEqual(rev_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("payments totaling", str(rev_res.data).lower())

    def test_sales_accounting_summary_metrics(self):
        # 1. Posted invoice
        inv1 = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 2, 1), total_amount=Decimal("1000.00"), status="open",
        )
        self.client.post(f"/api/accounting/sales/{inv1.id}/post/", {"tax_amount": "100.00"}, format="json")

        # 2. Unposted invoice
        Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 2, 5), total_amount=Decimal("500.00"), status="open",
        )

        res = self.client.get("/api/accounting/sales/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data

        self.assertEqual(data["total_invoices_count"], 2)
        self.assertEqual(Decimal(str(data["total_invoices_amount"])), Decimal("1500.00"))
        self.assertEqual(data["posted_invoices_count"], 1)
        self.assertEqual(Decimal(str(data["posted_invoices_amount"])), Decimal("1000.00"))
        self.assertEqual(data["unposted_invoices_count"], 1)
        self.assertEqual(Decimal(str(data["unposted_invoices_amount"])), Decimal("500.00"))
        self.assertEqual(Decimal(str(data["gl_sales_revenue"])), Decimal("900.00"))
        self.assertEqual(Decimal(str(data["gl_tax_payable"])), Decimal("100.00"))
        self.assertEqual(Decimal(str(data["gl_accounts_receivable"])), Decimal("1000.00"))

    def test_sales_accounting_invoices_list(self):
        inv1 = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 2, 1), total_amount=Decimal("1000.00"), status="open",
        )
        self.client.post(f"/api/accounting/sales/{inv1.id}/post/", {}, format="json")

        inv2 = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 2, 5), total_amount=Decimal("500.00"), status="open",
        )

        res = self.client.get("/api/accounting/sales/invoices/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = {item["id"]: item for item in res.data}

        self.assertEqual(items[inv1.id]["accounting_status"], "posted")
        self.assertIsNotNone(items[inv1.id]["journal_entry"])
        self.assertEqual(items[inv2.id]["accounting_status"], "not_posted")
        self.assertIsNone(items[inv2.id]["journal_entry"])

    def test_locked_period_blocks_sales_invoice_posting(self):
        # Close Period 02 (February 2026)
        p2 = AccountingPeriod.objects.get(fiscal_year__company=self.comp1, period_number=2)
        p2.status = "closed"
        p2.save()

        inv = Invoice.objects.create(
            company=self.comp1, customer=self.customer1,
            invoice_date=date(2026, 2, 10), total_amount=Decimal("1000.00"), status="open",
        )

        res = self.client.post(f"/api/accounting/sales/{inv.id}/post/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("closed", str(res.data).lower())

    def test_multi_tenant_isolation_sales_accounting(self):
        inv_comp2 = Invoice.objects.create(
            company=self.comp2, customer=self.customer_comp2,
            invoice_date=date(2026, 2, 10), total_amount=Decimal("900.00"), status="open",
        )

        # Company 1 finance user cannot post Company 2 invoice
        res = self.client.post(f"/api/accounting/sales/{inv_comp2.id}/post/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not found", str(res.data).lower())

        # Company 1 list does not include Company 2 invoice
        list_res = self.client.get("/api/accounting/sales/invoices/")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        comp1_inv_ids = [item["id"] for item in list_res.data]
        self.assertNotIn(inv_comp2.id, comp1_inv_ids)

    def test_unauthorized_user_forbidden(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/sales/summary/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class PurchaseAccountingTests(APITestCase):
    """
    Test suite for Master Accounting Blueprint Section #13: Purchase-to-Accounting.
    Covers operational vendor bill posting, preview calculation, material/expense allocation,
    input tax recoverable, reversal/cancellation, GL integration, and security controls.
    """

    def setUp(self):
        # 1. Tenants
        self.comp1 = Company.objects.create(name="Brewing Master Co", slug="brewmaster")
        self.comp2 = Company.objects.create(name="Competitor Distilling", slug="compdist")

        # 2. Users
        self.finance_user = User.objects.create_user(
            username="purchfin",
            email="fin@purchmaster.com",
            role="finance",
            company=self.comp1,
            password="pass"
        )
        self.store_user = User.objects.create_user(
            username="purchstore",
            email="store@purchmaster.com",
            role="store_manager",
            company=self.comp1,
            password="pass"
        )
        self.comp2_user = User.objects.create_user(
            username="comp2purchfin",
            email="fin@compdistpurch.com",
            role="finance",
            company=self.comp2,
            password="pass"
        )

        # 3. Seed Accounts & Fiscal Year
        seed_standard_chart_of_accounts(self.comp1)
        seed_standard_fiscal_year(self.comp1, 2026)
        seed_standard_chart_of_accounts(self.comp2)
        seed_standard_fiscal_year(self.comp2, 2026)

        self.acc_ap = Account.objects.get(company=self.comp1, code="2010")
        self.acc_inv = Account.objects.get(company=self.comp1, code="1210")
        self.acc_bank = Account.objects.get(company=self.comp1, code="1010")

        # 4. Vendors
        from procurement.models import Vendor, Bill
        self.vendor1 = Vendor.objects.create(
            company=self.comp1,
            name="Apex Hops & Barley Ltd",
            email="hops@barley.com",
            payment_terms="Net 30",
        )
        self.vendor_comp2 = Vendor.objects.create(
            company=self.comp2,
            name="Other Hops Corp",
            email="other@hops.com",
        )

        self.client.force_authenticate(user=self.finance_user)

    def test_purchase_accounting_preview_endpoint(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-PRV-100",
            bill_date=date(2026, 2, 10),
            due_date=date(2026, 3, 12),
            total_amount=Decimal("1200.00"),
            status="open",
        )

        # Preview with tax
        res = self.client.post(f"/api/accounting/purchases/{bill.id}/preview/", {
            "tax_amount": "120.00"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data
        self.assertTrue(data["is_balanced"])
        self.assertEqual(data["total_debit"], 1200.00)
        self.assertEqual(data["total_credit"], 1200.00)
        self.assertEqual(data["net_purchase"], 1080.00)
        self.assertEqual(data["tax_amount"], 120.00)
        self.assertEqual(len(data["lines"]), 3)

    def test_post_purchase_bill_creates_balanced_journal_entry(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-BAL-200",
            bill_date=date(2026, 2, 10),
            total_amount=Decimal("1500.00"),
            status="open",
        )

        res = self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIn("journal_entry_id", res.data)

        je = JournalEntry.objects.get(pk=res.data["journal_entry_id"])
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.total_debit, Decimal("1500.00"))
        self.assertEqual(je.total_credit, Decimal("1500.00"))

        # Verify lines
        lines = list(je.lines.order_by("line_number"))
        self.assertEqual(len(lines), 2)
        # Line 1: Purchase / Inventory Debit
        self.assertEqual(lines[0].account.code, "1210")
        self.assertEqual(lines[0].debit, Decimal("1500.00"))
        self.assertEqual(lines[0].credit, Decimal("0.00"))
        # Line 2: AP Credit
        self.assertEqual(lines[1].account.code, "2010")
        self.assertEqual(lines[1].debit, Decimal("0.00"))
        self.assertEqual(lines[1].credit, Decimal("1500.00"))

        # Vendor balance synced
        self.vendor1.refresh_from_db()
        self.assertEqual(self.vendor1.outstanding_balance, Decimal("1500.00"))

    def test_post_purchase_bill_with_tax(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-TAX-300",
            bill_date=date(2026, 2, 15),
            total_amount=Decimal("1100.00"),
            status="open",
        )

        res = self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {
            "tax_amount": "100.00"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        je = JournalEntry.objects.get(pk=res.data["journal_entry_id"])
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.total_debit, Decimal("1100.00"))
        self.assertEqual(je.total_credit, Decimal("1100.00"))

        lines = list(je.lines.order_by("line_number"))
        self.assertEqual(len(lines), 3)

        # Line 1: Raw Materials Debit = 1000
        self.assertEqual(lines[0].account.code, "1210")
        self.assertEqual(lines[0].debit, Decimal("1000.00"))
        self.assertEqual(lines[0].credit, Decimal("0.00"))

        # Line 2: Input Tax Recoverable Debit = 100
        self.assertEqual(lines[1].account.code, "1310")
        self.assertEqual(lines[1].debit, Decimal("100.00"))
        self.assertEqual(lines[1].credit, Decimal("0.00"))

        # Line 3: Accounts Payable Credit = 1100
        self.assertEqual(lines[2].account.code, "2010")
        self.assertEqual(lines[2].debit, Decimal("0.00"))
        self.assertEqual(lines[2].credit, Decimal("1100.00"))

    def test_duplicate_purchase_bill_posting_is_prevented(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-DUP-400",
            bill_date=date(2026, 2, 12),
            total_amount=Decimal("750.00"),
            status="open",
        )

        res1 = self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        res2 = self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been posted", str(res2.data).lower())

    def test_posted_purchase_bill_flows_into_general_ledger(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-GL-500",
            bill_date=date(2026, 2, 18),
            total_amount=Decimal("2000.00"),
            status="open",
        )

        self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {
            "tax_amount": "200.00"
        }, format="json")

        # 1. GL Summary
        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        balances = {item["code"]: item for item in gl_res.data["accounts"]}

        # Inventory (1210): Debit 1800, closing 1800
        self.assertEqual(Decimal(balances["1210"]["period_debit"]), Decimal("1800.00"))
        self.assertEqual(Decimal(balances["1210"]["closing_balance"]), Decimal("1800.00"))

        # Input Tax (1310): Debit 200, closing 200
        self.assertEqual(Decimal(balances["1310"]["period_debit"]), Decimal("200.00"))
        self.assertEqual(Decimal(balances["1310"]["closing_balance"]), Decimal("200.00"))

        # Accounts Payable (2010): Credit 2000, closing 2000
        self.assertEqual(Decimal(balances["2010"]["period_credit"]), Decimal("2000.00"))
        self.assertEqual(Decimal(balances["2010"]["closing_balance"]), Decimal("2000.00"))

    def test_draft_or_unposted_purchase_bill_does_not_affect_gl(self):
        from procurement.models import Bill
        Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-DRAFT-600",
            bill_date=date(2026, 2, 20),
            total_amount=Decimal("3500.00"),
            status="open",
        )

        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        self.assertEqual(gl_res.status_code, status.HTTP_200_OK)
        balances = {item["code"]: item for item in gl_res.data["accounts"]}
        self.assertEqual(Decimal(balances["2010"]["closing_balance"]), Decimal("0.00"))
        self.assertEqual(Decimal(balances["1210"]["closing_balance"]), Decimal("0.00"))

    def test_reverse_purchase_bill_accounting(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-REV-700",
            bill_date=date(2026, 2, 22),
            total_amount=Decimal("800.00"),
            status="open",
        )

        post_res = self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")
        self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)

        # Now reverse
        rev_res = self.client.post(f"/api/accounting/purchases/{bill.id}/reverse/", {
            "reason": "Damaged goods returned to vendor"
        }, format="json")
        self.assertEqual(rev_res.status_code, status.HTTP_200_OK)
        self.assertEqual(rev_res.data["status"], "cancelled")

        bill.refresh_from_db()
        self.assertEqual(bill.status, "cancelled")

        # Check reversing journal entry
        reversal_je_id = rev_res.data["reversal_journal_entry_id"]
        reversal_je = JournalEntry.objects.get(pk=reversal_je_id)
        self.assertEqual(reversal_je.status, "posted")
        self.assertEqual(reversal_je.total_debit, Decimal("800.00"))
        self.assertEqual(reversal_je.total_credit, Decimal("800.00"))

        # GL Net Balance for AP and Inventory should now be 0
        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        balances = {item["code"]: item for item in gl_res.data["accounts"]}
        self.assertEqual(Decimal(balances["2010"]["closing_balance"]), Decimal("0.00"))
        self.assertEqual(Decimal(balances["1210"]["closing_balance"]), Decimal("0.00"))

        # Vendor balance synced back to 0
        self.vendor1.refresh_from_db()
        self.assertEqual(self.vendor1.outstanding_balance, Decimal("0.00"))

    def test_reversal_blocked_if_payments_already_applied(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-PMT-800",
            bill_date=date(2026, 2, 25),
            total_amount=Decimal("1000.00"),
            status="open",
        )
        self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")

        # Apply a payment
        bill.apply_payment(Decimal("300.00"))

        rev_res = self.client.post(f"/api/accounting/purchases/{bill.id}/reverse/", {
            "reason": "Invalid attempt"
        }, format="json")
        self.assertEqual(rev_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("payments totaling", str(rev_res.data).lower())

    def test_purchase_bill_settlement_with_ap_payment(self):
        from procurement.models import Bill
        bill = Bill.objects.create(
            company=self.comp1,
            vendor=self.vendor1,
            bill_number="BILL-SETTLE-900",
            bill_date=date(2026, 2, 10),
            total_amount=Decimal("1000.00"),
            status="open",
        )
        self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")

        # Settle via existing AP payment mechanism
        pay_res = self.client.post("/api/accounting/payables/record-payment/", {
            "vendor_id": self.vendor1.id,
            "amount": "1000.00",
            "bill_id": bill.id,
            "method": "bank_transfer",
        }, format="json")
        self.assertEqual(pay_res.status_code, status.HTTP_201_CREATED)

        bill.refresh_from_db()
        self.assertEqual(bill.status, "paid")
        self.assertEqual(bill.balance_due, Decimal("0.00"))

        self.vendor1.refresh_from_db()
        self.assertEqual(self.vendor1.outstanding_balance, Decimal("0.00"))

        # GL AP balance should return to 0
        gl_res = self.client.get("/api/accounting/general-ledger/summary/")
        balances = {item["code"]: item for item in gl_res.data["accounts"]}
        self.assertEqual(Decimal(balances["2010"]["closing_balance"]), Decimal("0.00"))

    def test_purchase_accounting_summary_metrics(self):
        from procurement.models import Bill
        # 1. Posted bill
        bill1 = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-SUM-1",
            bill_date=date(2026, 2, 1), total_amount=Decimal("1000.00"), status="open",
        )
        self.client.post(f"/api/accounting/purchases/{bill1.id}/post/", {"tax_amount": "100.00"}, format="json")

        # 2. Unposted bill
        Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-SUM-2",
            bill_date=date(2026, 2, 5), total_amount=Decimal("500.00"), status="open",
        )

        res = self.client.get("/api/accounting/purchases/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.data

        self.assertEqual(data["total_bills_count"], 2)
        self.assertEqual(Decimal(str(data["total_bills_amount"])), Decimal("1500.00"))
        self.assertEqual(data["posted_bills_count"], 1)
        self.assertEqual(Decimal(str(data["posted_bills_amount"])), Decimal("1000.00"))
        self.assertEqual(data["unposted_bills_count"], 1)
        self.assertEqual(Decimal(str(data["unposted_bills_amount"])), Decimal("500.00"))
        self.assertEqual(Decimal(str(data["gl_purchase_expense"])), Decimal("900.00"))
        self.assertEqual(Decimal(str(data["gl_input_tax"])), Decimal("100.00"))
        self.assertEqual(Decimal(str(data["gl_accounts_payable"])), Decimal("1000.00"))

    def test_purchase_accounting_bills_list(self):
        from procurement.models import Bill
        bill1 = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-LIST-1",
            bill_date=date(2026, 2, 1), total_amount=Decimal("1000.00"), status="open",
        )
        self.client.post(f"/api/accounting/purchases/{bill1.id}/post/", {}, format="json")

        bill2 = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-LIST-2",
            bill_date=date(2026, 2, 5), total_amount=Decimal("500.00"), status="open",
        )

        res = self.client.get("/api/accounting/purchases/bills/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = {item["id"]: item for item in res.data}

        self.assertEqual(items[bill1.id]["accounting_status"], "posted")
        self.assertIsNotNone(items[bill1.id]["journal_entry"])
        self.assertEqual(items[bill2.id]["accounting_status"], "not_posted")
        self.assertIsNone(items[bill2.id]["journal_entry"])

    def test_locked_period_blocks_purchase_bill_posting(self):
        from procurement.models import Bill
        # Close Period 02 (February 2026)
        p2 = AccountingPeriod.objects.get(fiscal_year__company=self.comp1, period_number=2)
        p2.status = "closed"
        p2.save()

        bill = Bill.objects.create(
            company=self.comp1, vendor=self.vendor1,
            bill_number="BILL-LCK-1",
            bill_date=date(2026, 2, 10), total_amount=Decimal("1000.00"), status="open",
        )

        res = self.client.post(f"/api/accounting/purchases/{bill.id}/post/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("closed", str(res.data).lower())

    def test_multi_tenant_isolation_purchase_accounting(self):
        from procurement.models import Bill
        bill_comp2 = Bill.objects.create(
            company=self.comp2, vendor=self.vendor_comp2,
            bill_number="BILL-COMP2-1",
            bill_date=date(2026, 2, 10), total_amount=Decimal("900.00"), status="open",
        )

        # Company 1 finance user cannot post Company 2 bill
        res = self.client.post(f"/api/accounting/purchases/{bill_comp2.id}/post/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not found", str(res.data).lower())

        # Company 1 list does not include Company 2 bill
        list_res = self.client.get("/api/accounting/purchases/bills/")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        comp1_bill_ids = [item["id"] for item in list_res.data]
        self.assertNotIn(bill_comp2.id, comp1_bill_ids)

    def test_unauthorized_user_forbidden(self):
        self.client.force_authenticate(user=self.store_user)
        res = self.client.get("/api/accounting/purchases/summary/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


from inventory.models import Item, Warehouse, Stock, StockMovement


class InventoryAccountingTests(TestCase):
    """
    Automated Unit & Integration Tests for Blueprint Section #14: Inventory-to-Accounting.
    Validates receipt, issue, transfer, adjustment, write-off, revaluation, idempotency,
    reversals, and General Ledger posting.
    """
    def setUp(self):
        self.company = Company.objects.create(name="Brew Craft Co", slug="brewcraft")
        ensure_account_types()
        seed_standard_chart_of_accounts(self.company)
        self.fy = seed_standard_fiscal_year(self.company, 2026)

        self.user = User.objects.create_user(
            username="inventory_accountant",
            email="inv@brewcraft.com",
            role="finance",
            company=self.company,
        )

        self.warehouse = Warehouse.objects.create(
            company=self.company,
            name="Main Plant Warehouse",
            location="Building A",
        )
        self.secondary_warehouse = Warehouse.objects.create(
            company=self.company,
            name="Cold Storage Facility",
            location="Building C",
        )

        self.raw_item = Item.objects.create(
            company=self.company,
            name="Organic Barley Malt",
            category="raw_material",
            unit="kg",
            purchase_cost=Decimal("5.00"),
            selling_price=Decimal("0.00"),
        )
        self.finished_item = Item.objects.create(
            company=self.company,
            name="Craft IPA 6-Pack",
            category="finished_good",
            unit="case",
            purchase_cost=Decimal("12.00"),
            selling_price=Decimal("24.00"),
        )

    def test_inventory_receipt_creates_balanced_journal(self):
        from inventory.services import increase_stock
        from accounting.inventory_accounting import (
            determine_movement_accounting_requirement,
            post_inventory_movement_to_accounting,
        )

        # Operational receipt: 100 kg at $5.00 = $500.00
        increase_stock(self.raw_item, self.warehouse, 100, user=self.user, reference="Vendor Delivery PO#1001")
        movement = StockMovement.objects.filter(item=self.raw_item).latest("created_at")

        requires, reason, event_subtype = determine_movement_accounting_requirement(movement, self.company)
        self.assertTrue(requires)
        self.assertEqual(event_subtype, "receipt")

        je = post_inventory_movement_to_accounting(movement.id, user=self.user, company=self.company)
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.source_module, "inventory")
        self.assertEqual(je.source_id, movement.id)
        self.assertEqual(je.lines.count(), 2)

        dr_line = je.lines.filter(debit__gt=0).first()
        cr_line = je.lines.filter(credit__gt=0).first()

        self.assertEqual(dr_line.account.code, "1210")  # Raw Materials Inventory Asset
        self.assertEqual(dr_line.debit, Decimal("500.00"))
        self.assertIn(cr_line.account.code, ["2010", "2020"])  # Clearing or AP Trade
        self.assertEqual(cr_line.credit, Decimal("500.00"))

    def test_finished_goods_receipt_uses_finished_goods_asset_account(self):
        from inventory.services import increase_stock
        from accounting.inventory_accounting import post_inventory_movement_to_accounting

        # Production receipt: 50 cases of finished goods at $12.00 = $600.00
        increase_stock(self.finished_item, self.warehouse, 50, user=self.user, reference="Finished Batch #B-101")
        movement = StockMovement.objects.filter(item=self.finished_item).latest("created_at")

        je = post_inventory_movement_to_accounting(movement.id, user=self.user, company=self.company)
        self.assertEqual(je.status, "posted")

        dr_line = je.lines.filter(debit__gt=0).first()
        self.assertEqual(dr_line.account.code, "1230")  # Finished Goods Inventory
        self.assertEqual(dr_line.debit, Decimal("600.00"))

    def test_inventory_issue_consumption_creates_cogs_entry(self):
        from inventory.services import increase_stock, decrease_stock
        from accounting.inventory_accounting import post_inventory_movement_to_accounting

        # Inward stock first
        increase_stock(self.raw_item, self.warehouse, 200, user=self.user, reference="Initial Stock")
        # Issue for batch production: 40 kg at $5.00 = $200.00
        decrease_stock(self.raw_item, self.warehouse, 40, user=self.user, reference="Batch Production #201")
        out_movement = StockMovement.objects.filter(item=self.raw_item, movement_type="OUT").latest("created_at")

        je = post_inventory_movement_to_accounting(out_movement.id, user=self.user, company=self.company)
        self.assertEqual(je.status, "posted")

        dr_line = je.lines.filter(debit__gt=0).first()
        cr_line = je.lines.filter(credit__gt=0).first()

        self.assertEqual(dr_line.account.code, "5010")  # Direct Raw Materials Consumed (COGS)
        self.assertEqual(dr_line.debit, Decimal("200.00"))
        self.assertEqual(cr_line.account.code, "1210")  # Raw Materials Inventory Asset
        self.assertEqual(cr_line.credit, Decimal("200.00"))

    def test_internal_warehouse_transfer_does_not_create_redundant_gl_entry(self):
        from inventory.services import increase_stock
        from inventory.views import StockViewSet
        from accounting.inventory_accounting import (
            determine_movement_accounting_requirement,
            post_inventory_movement_to_accounting,
        )

        increase_stock(self.raw_item, self.warehouse, 100, user=self.user, reference="Initial Stock")
        # Simulate transfer out movement
        transfer_out = StockMovement.objects.create(
            item=self.raw_item,
            warehouse=self.warehouse,
            movement_type="OUT",
            quantity=30,
            reference=f"Transfer to {self.secondary_warehouse.name}",
            created_by=self.user,
        )

        requires, reason, event_subtype = determine_movement_accounting_requirement(transfer_out, self.company)
        self.assertFalse(requires)
        self.assertEqual(event_subtype, "transfer_internal")

        # Posting an internal transfer movement raises ValidationError
        with self.assertRaises(ValidationError):
            post_inventory_movement_to_accounting(transfer_out.id, user=self.user, company=self.company)

    def test_positive_and_negative_inventory_adjustments(self):
        from inventory.services import increase_stock, adjust_stock
        from accounting.inventory_accounting import post_inventory_movement_to_accounting

        increase_stock(self.raw_item, self.warehouse, 50, user=self.user, reference="Initial Stock")

        # 1. Positive adjustment (Found 10 extra: 50 -> 60, diff = +10 at $5 = $50 gain)
        adjust_stock(self.raw_item, self.warehouse, 60, user=self.user, reference="Physical Count Variance Gain")
        pos_mov = StockMovement.objects.filter(item=self.raw_item, movement_type="ADJUST").latest("created_at")

        je_pos = post_inventory_movement_to_accounting(pos_mov.id, user=self.user, company=self.company)
        self.assertEqual(je_pos.status, "posted")
        dr_pos = je_pos.lines.filter(debit__gt=0).first()
        cr_pos = je_pos.lines.filter(credit__gt=0).first()
        self.assertEqual(dr_pos.account.code, "1210")  # DR Asset
        self.assertEqual(dr_pos.debit, Decimal("50.00"))
        self.assertEqual(cr_pos.account.code, "5090")  # CR Variance Gain
        self.assertEqual(cr_pos.credit, Decimal("50.00"))

        # 2. Negative adjustment (Shrinkage: 60 -> 45, diff = -15 at $5 = $75 loss)
        adjust_stock(self.raw_item, self.warehouse, 45, user=self.user, reference="Physical Count Variance Loss")
        neg_mov = StockMovement.objects.filter(item=self.raw_item, movement_type="ADJUST").latest("created_at")

        je_neg = post_inventory_movement_to_accounting(neg_mov.id, user=self.user, company=self.company)
        self.assertEqual(je_neg.status, "posted")
        dr_neg = je_neg.lines.filter(debit__gt=0).first()
        cr_neg = je_neg.lines.filter(credit__gt=0).first()
        self.assertEqual(dr_neg.account.code, "5090")  # DR Adjustment Loss
        self.assertEqual(dr_neg.debit, Decimal("75.00"))
        self.assertEqual(cr_neg.account.code, "1210")  # CR Asset
        self.assertEqual(cr_neg.credit, Decimal("75.00"))

    def test_inventory_write_off_creates_loss_expense_entry(self):
        from inventory.services import increase_stock, decrease_stock
        from accounting.inventory_accounting import post_inventory_movement_to_accounting

        increase_stock(self.raw_item, self.warehouse, 50, user=self.user, reference="Initial Stock")
        # Damaged stock write-off: 10 kg at $5.00 = $50.00
        decrease_stock(self.raw_item, self.warehouse, 10, user=self.user, reference="Damaged moisture write-off")
        write_off_mov = StockMovement.objects.filter(item=self.raw_item, movement_type="OUT").latest("created_at")

        je = post_inventory_movement_to_accounting(write_off_mov.id, user=self.user, company=self.company)
        self.assertEqual(je.status, "posted")
        dr_line = je.lines.filter(debit__gt=0).first()
        cr_line = je.lines.filter(credit__gt=0).first()
        self.assertEqual(dr_line.account.code, "6520")  # Inventory Loss & Write-off Expense
        self.assertEqual(dr_line.debit, Decimal("50.00"))
        self.assertEqual(cr_line.account.code, "1210")  # Inventory Asset
        self.assertEqual(cr_line.credit, Decimal("50.00"))

    def test_inventory_revaluation_event(self):
        from inventory.services import increase_stock
        from accounting.inventory_accounting import post_inventory_valuation_event

        # 100 kg on-hand at old cost $5.00 = $500.00
        increase_stock(self.raw_item, self.warehouse, 100, user=self.user, reference="Initial Stock")

        # Revalue to $6.50: New value = $650.00, Delta = +$150.00
        res = post_inventory_valuation_event(
            item_id=self.raw_item.id,
            new_unit_cost=Decimal("6.50"),
            user=self.user,
            company=self.company,
            reason="Market price index revaluation",
        )
        self.assertEqual(res["delta_amount"], 150.0)
        self.raw_item.refresh_from_db()
        self.assertEqual(self.raw_item.purchase_cost, Decimal("6.50"))

        je = JournalEntry.objects.get(id=res["journal_entry_id"])
        self.assertEqual(je.source_module, "inventory.valuation")
        self.assertEqual(je.lines.count(), 2)
        dr_line = je.lines.filter(debit__gt=0).first()
        cr_line = je.lines.filter(credit__gt=0).first()
        self.assertEqual(dr_line.account.code, "1210")
        self.assertEqual(dr_line.debit, Decimal("150.00"))
        self.assertEqual(cr_line.account.code, "5090")
        self.assertEqual(cr_line.credit, Decimal("150.00"))

        # Zero delta is rejected
        with self.assertRaises(ValidationError):
            post_inventory_valuation_event(
                item_id=self.raw_item.id,
                new_unit_cost=Decimal("6.50"),
                user=self.user,
                company=self.company,
            )

    def test_idempotency_prevents_duplicate_posting(self):
        from inventory.services import increase_stock
        from accounting.inventory_accounting import post_inventory_movement_to_accounting

        increase_stock(self.raw_item, self.warehouse, 20, user=self.user, reference="Inward Batch")
        movement = StockMovement.objects.filter(item=self.raw_item).latest("created_at")

        # First post succeeds
        je1 = post_inventory_movement_to_accounting(movement.id, user=self.user, company=self.company)
        self.assertEqual(je1.status, "posted")

        # Second post must raise ValidationError
        with self.assertRaises(ValidationError):
            post_inventory_movement_to_accounting(movement.id, user=self.user, company=self.company)

        # Journal count for this movement is exactly 1
        self.assertEqual(
            JournalEntry.objects.filter(company=self.company, source_module="inventory", source_id=movement.id).count(),
            1
        )

    def test_inventory_reversal_creates_mirrored_reversal_entry(self):
        from inventory.services import increase_stock
        from accounting.inventory_accounting import (
            post_inventory_movement_to_accounting,
            reverse_inventory_accounting,
        )

        increase_stock(self.raw_item, self.warehouse, 30, user=self.user, reference="To Reverse")
        movement = StockMovement.objects.filter(item=self.raw_item).latest("created_at")

        je = post_inventory_movement_to_accounting(movement.id, user=self.user, company=self.company)
        self.assertEqual(je.status, "posted")

        res = reverse_inventory_accounting(movement.id, user=self.user, company=self.company, reason="Incorrect goods receipt")
        self.assertEqual(res["status"], "reversed")
        reversal_je = res["reversal_journal_entry"]
        self.assertEqual(reversal_je.status, "posted")
        self.assertEqual(reversal_je.reversal_of, je)

        # Original journal marked reversed
        je.refresh_from_db()
        self.assertEqual(je.status, "reversed")

    def test_accounting_disabled_policy(self):
        from inventory.services import increase_stock
        from accounting.inventory_accounting import (
            determine_movement_accounting_requirement,
            post_inventory_movement_to_accounting,
        )

        # Disable inventory accounting in settings
        settings = AccountingSettings.objects.get(company=self.company)
        settings.inventory_accounting_enabled = False
        settings.save()

        increase_stock(self.raw_item, self.warehouse, 50, user=self.user, reference="When Disabled")
        movement = StockMovement.objects.filter(item=self.raw_item).latest("created_at")

        requires, reason, _ = determine_movement_accounting_requirement(movement, self.company)
        self.assertFalse(requires)
        self.assertIn("disabled", reason.lower())

        with self.assertRaises(ValidationError):
            post_inventory_movement_to_accounting(movement.id, user=self.user, company=self.company)


class InventoryAccountingAPITests(APITestCase):
    """
    Integration tests for Section #14 REST API endpoints.
    """
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name="Apex Brewing", slug="apexbrew")
        ensure_account_types()
        seed_standard_chart_of_accounts(self.company)
        self.fy = seed_standard_fiscal_year(self.company, 2026)

        self.user = User.objects.create_user(
            username="finance_officer",
            email="finance@apexbrew.com",
            role="finance",
            company=self.company,
        )
        self.client.force_authenticate(user=self.user)

        self.warehouse = Warehouse.objects.create(
            company=self.company,
            name="Apex Main Warehouse",
            location="Zone 1",
        )
        self.item = Item.objects.create(
            company=self.company,
            name="Crystal Hops",
            category="raw_material",
            unit="kg",
            purchase_cost=Decimal("15.00"),
        )

        from inventory.services import increase_stock
        increase_stock(self.item, self.warehouse, 20, user=self.user, reference="GRN-8801")
        self.movement = StockMovement.objects.filter(item=self.item).latest("created_at")

    def test_inventory_accounting_summary_endpoint(self):
        res = self.client.get("/api/accounting/inventory/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("total_inventory_gl_value", res.data)
        self.assertIn("total_movements_count", res.data)
        self.assertIn("policy", res.data)

    def test_inventory_movements_endpoint(self):
        res = self.client.get("/api/accounting/inventory/movements/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(len(res.data) >= 1)
        first = res.data[0]
        self.assertEqual(first["id"], self.movement.id)
        self.assertEqual(first["accounting_status"], "pending")
        self.assertEqual(first["valuation_amount"], 300.0)

    def test_inventory_preview_and_post_flow(self):
        # 1. Preview
        prev_res = self.client.post(f"/api/accounting/inventory/{self.movement.id}/preview/")
        self.assertEqual(prev_res.status_code, status.HTTP_200_OK)
        self.assertEqual(prev_res.data["valuation_amount"], 300.0)
        self.assertTrue(prev_res.data["is_balanced"])
        self.assertEqual(len(prev_res.data["lines"]), 2)

        # 2. Post
        post_res = self.client.post(f"/api/accounting/inventory/{self.movement.id}/post/")
        self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(post_res.data["status"], "posted")
        je_id = post_res.data["journal_entry_id"]

        # 3. Check movement status updated
        mov_res = self.client.get("/api/accounting/inventory/movements/")
        self.assertEqual(mov_res.status_code, status.HTTP_200_OK)
        updated = next(m for m in mov_res.data if m["id"] == self.movement.id)
        self.assertEqual(updated["accounting_status"], "posted")
        self.assertEqual(updated["journal_entry_id"], je_id)

        # 4. Duplicate post rejected
        dup_res = self.client.post(f"/api/accounting/inventory/{self.movement.id}/post/")
        self.assertEqual(dup_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been posted", str(dup_res.data).lower())

        # 5. Reverse
        rev_res = self.client.post(f"/api/accounting/inventory/{self.movement.id}/reverse/", {"reason": "Test reversal"})
        self.assertEqual(rev_res.status_code, status.HTTP_200_OK)
        self.assertEqual(rev_res.data["status"], "reversed")

    def test_inventory_revaluation_endpoint(self):
        # On-hand is 20 kg at $15.00 = $300.00. Revalue to $18.00 = $360.00, Delta = $60.00
        res = self.client.post("/api/accounting/inventory/revalue/", {
            "item_id": self.item.id,
            "new_unit_cost": "18.00",
            "reason": "Quarterly market adjustment",
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["delta_amount"], 60.0)
        self.assertEqual(res.data["new_unit_cost"], 18.0)


# ==============================================================================
# SECTION #15: MANUFACTURING-TO-ACCOUNTING INTEGRATION TESTS
# ==============================================================================

class ManufacturingAccountingTests(TestCase):
    """
    Unit & integration tests for Blueprint Section No. 15 (Manufacturing-to-Accounting).
    Tests production order cost calculation, BOM material consumption, direct labor,
    applied overhead, WIP asset accumulation, finished goods completion, scrap loss,
    cost variance adjustments, idempotency, and audit traceability.
    """

    def setUp(self):
        self.company = Company.objects.create(name="BrewCraft Manufacturing Corp", slug="brewcraft-mfg")
        ensure_account_types()
        self.user = User.objects.create_user(
            username="mfg_accountant",
            email="mfg@brewcraft.com",
            password="password123",
            company=self.company,
        )
        self.fy = seed_standard_fiscal_year(self.company, 2026)
        seed_standard_chart_of_accounts(self.company)

        self.settings = AccountingSettings.objects.get(company=self.company)
        self.settings.manufacturing_accounting_enabled = True
        self.settings.wip_accounting_enabled = True
        self.settings.labor_accounting_enabled = True
        self.settings.overhead_accounting_enabled = True
        self.settings.labor_rate_per_unit = Decimal("2.00")
        self.settings.overhead_rate_per_unit = Decimal("1.50")
        self.settings.save()

        self.warehouse = Warehouse.objects.create(company=self.company, name="Mount Kisco Production Facility")
        
        # Raw materials
        self.malt = Item.objects.create(
            name="Organic Barley Malt",
            sku="RAW-MALT-01",
            category="raw_material",
            purchase_cost=Decimal("4.00"),
            company=self.company,
        )
        self.hops = Item.objects.create(
            name="Cascade Hops",
            sku="RAW-HOPS-01",
            category="raw_material",
            purchase_cost=Decimal("10.00"),
            company=self.company,
        )
        # Finished Good
        self.beer = Item.objects.create(
            name="Craft Amber Ale 24pk",
            sku="FG-ALE-24",
            category="finished_good",
            selling_price=Decimal("45.00"),
            company=self.company,
        )

        # Recipe: 1 batch = 50 units
        # Consumes 20 kg malt and 2 kg hops per batch
        self.recipe = Recipe.objects.create(product=self.beer, batch_size=50)
        self.ing1 = RecipeIngredient.objects.create(recipe=self.recipe, item=self.malt, quantity=20.0)
        self.ing2 = RecipeIngredient.objects.create(recipe=self.recipe, item=self.hops, quantity=2.0)

        # Production Order: 100 units (2 batches)
        # Required: 40 kg malt ($160.00) + 4 kg hops ($40.00) = $200.00 Material Cost
        # Labor: 100 * $2.00 = $200.00
        # Overhead: 100 * $1.50 = $150.00
        # Total Production Cost: $550.00 ($5.50/unit)
        self.order = ProductionOrder.objects.create(
            recipe=self.recipe,
            quantity=100.0,
            warehouse=self.warehouse,
            status="completed",
        )

    def test_manufacturing_accounting_policy_resolution(self):
        from accounting.manufacturing_accounting import (
            get_manufacturing_policy,
            resolve_manufacturing_wip_account,
            resolve_manufacturing_raw_material_account,
            resolve_manufacturing_finished_goods_account,
            resolve_manufacturing_labor_account,
            resolve_manufacturing_overhead_account,
            resolve_manufacturing_scrap_account,
            resolve_manufacturing_variance_account,
        )
        policy = get_manufacturing_policy(self.company)
        self.assertTrue(policy["enabled"])
        self.assertTrue(policy["wip_enabled"])
        self.assertTrue(policy["labor_enabled"])
        self.assertTrue(policy["overhead_enabled"])
        self.assertEqual(policy["labor_rate_per_unit"], Decimal("2.00"))
        self.assertEqual(policy["overhead_rate_per_unit"], Decimal("1.50"))

        wip = resolve_manufacturing_wip_account(self.company)
        self.assertEqual(wip.code, "1220")
        self.assertFalse(wip.is_header)

        raw = resolve_manufacturing_raw_material_account(self.malt, self.company)
        self.assertEqual(raw.code, "1210")

        fg = resolve_manufacturing_finished_goods_account(self.beer, self.company)
        self.assertEqual(fg.code, "1230")

        labor = resolve_manufacturing_labor_account(self.company)
        self.assertIn(labor.code, ["2100", "5100"])

        overhead = resolve_manufacturing_overhead_account(self.company)
        self.assertIn(overhead.code, ["5040", "5200"])

        scrap = resolve_manufacturing_scrap_account(self.company)
        self.assertIn(scrap.code, ["5080", "6520"])

        var = resolve_manufacturing_variance_account(self.company)
        self.assertEqual(var.code, "5090")

    def test_cost_calculation_from_bom_and_valuation(self):
        from accounting.manufacturing_accounting import calculate_production_order_costs

        costs = calculate_production_order_costs(self.order, self.company)
        self.assertEqual(costs["planned_quantity"], 100.0)
        self.assertEqual(costs["batches"], 2)
        self.assertEqual(costs["total_material_cost"], Decimal("200.00"))
        self.assertEqual(costs["labor_cost"], Decimal("200.00"))
        self.assertEqual(costs["overhead_cost"], Decimal("150.00"))
        self.assertEqual(costs["total_production_cost"], Decimal("550.00"))
        self.assertEqual(costs["unit_production_cost"], Decimal("5.5000"))

    def test_manufacturing_preview_and_posting_full_completion(self):
        from accounting.manufacturing_accounting import (
            get_manufacturing_accounting_preview,
            post_manufacturing_accounting,
        )

        preview = get_manufacturing_accounting_preview(self.order.id, self.company)
        self.assertTrue(preview["is_balanced"])
        self.assertEqual(preview["costs"]["total_production_cost"], 550.0)
        self.assertEqual(preview["costs"]["finished_goods_value"], 550.0)
        self.assertEqual(preview["costs"]["remaining_wip_balance"], 0.0)

        # Post to accounting
        je, created = post_manufacturing_accounting(
            self.order.id,
            self.company,
            user=self.user,
            notes="Full batch run posted",
        )
        self.assertTrue(created)
        self.assertEqual(je.status, "posted")
        self.assertEqual(je.source_module, "manufacturing")
        self.assertEqual(je.source_id, self.order.id)
        self.assertEqual(je.reference, f"MFG-PO-{self.order.id}")

        # Check GL lines
        lines = je.lines.all()
        # Finished Goods Inventory (1230) debited for $550.00
        fg_line = lines.filter(account__code="1230", debit__gt=0).first()
        self.assertIsNotNone(fg_line)
        self.assertEqual(fg_line.debit, Decimal("550.00"))

        # Raw Material Inventory (1210) credited for $200.00
        raw_lines_credit = lines.filter(account__code="1210").aggregate(s=Sum("credit"))["s"]
        self.assertEqual(raw_lines_credit, Decimal("200.00"))

        # Labor clearing credited for $200.00
        labor_cr = lines.filter(account__code__in=["2100", "5100"], credit__gt=0).first()
        self.assertIsNotNone(labor_cr)
        self.assertEqual(labor_cr.credit, Decimal("200.00"))

        # Overhead clearing credited for $150.00
        oh_cr = lines.filter(account__code__in=["5040", "5200"], credit__gt=0).first()
        self.assertIsNotNone(oh_cr)
        self.assertEqual(oh_cr.credit, Decimal("150.00"))

        # WIP lines: DR $550 (materials $200 + labor $200 + overhead $150) and CR $550 (to FG)
        wip_dr = lines.filter(account__code="1220").aggregate(s=Sum("debit"))["s"]
        wip_cr = lines.filter(account__code="1220").aggregate(s=Sum("credit"))["s"]
        self.assertEqual(wip_dr, Decimal("550.00"))
        self.assertEqual(wip_cr, Decimal("550.00"))

    def test_partial_production_completion_preserves_remaining_wip(self):
        from accounting.manufacturing_accounting import (
            get_manufacturing_accounting_preview,
            post_manufacturing_accounting,
        )

        # Complete only 60 units out of 100 planned
        # 60 units * $5.50/unit = $330.00 transferred to FG
        # Remaining 40 units * $5.50/unit = $220.00 preserved in WIP
        preview = get_manufacturing_accounting_preview(
            self.order.id,
            self.company,
            completed_qty=60.0,
        )
        self.assertTrue(preview["is_balanced"])
        self.assertEqual(preview["costs"]["finished_goods_value"], 330.0)
        self.assertEqual(preview["costs"]["remaining_wip_balance"], 220.0)

        je, created = post_manufacturing_accounting(
            self.order.id,
            self.company,
            user=self.user,
            completed_qty=60.0,
        )
        self.assertTrue(created)

        # FG debited for $330.00
        fg_line = je.lines.filter(account__code="1230", debit__gt=0).first()
        self.assertEqual(fg_line.debit, Decimal("330.00"))

        # WIP debited for full cost ($550.00) and credited only for completed portion ($330.00)
        wip_dr = je.lines.filter(account__code="1220").aggregate(s=Sum("debit"))["s"]
        wip_cr = je.lines.filter(account__code="1220").aggregate(s=Sum("credit"))["s"]
        self.assertEqual(wip_dr, Decimal("550.00"))
        self.assertEqual(wip_cr, Decimal("330.00"))
        # Net balance remaining in WIP = $220.00
        self.assertEqual(wip_dr - wip_cr, Decimal("220.00"))

    def test_production_scrap_loss_accounting(self):
        from accounting.manufacturing_accounting import (
            get_manufacturing_accounting_preview,
            post_manufacturing_accounting,
        )

        # 90 completed, 10 scrapped = 100 total
        # FG = 90 * $5.50 = $495.00
        # Scrap Loss = 10 * $5.50 = $55.00
        preview = get_manufacturing_accounting_preview(
            self.order.id,
            self.company,
            completed_qty=90.0,
            scrap_qty=10.0,
        )
        self.assertTrue(preview["is_balanced"])
        self.assertEqual(preview["costs"]["finished_goods_value"], 495.0)
        self.assertEqual(preview["costs"]["scrap_value"], 55.0)

        je, created = post_manufacturing_accounting(
            self.order.id,
            self.company,
            user=self.user,
            completed_qty=90.0,
            scrap_qty=10.0,
        )
        self.assertTrue(created)

        scrap_line = je.lines.filter(account__code__in=["5080", "6520"], debit__gt=0).first()
        self.assertIsNotNone(scrap_line)
        self.assertEqual(scrap_line.debit, Decimal("55.00"))

    def test_manufacturing_variance_adjustment(self):
        from accounting.manufacturing_accounting import post_manufacturing_variance_adjustment

        # Unfavorable variance of $25.00
        var_je = post_manufacturing_variance_adjustment(
            self.order.id,
            self.company,
            user=self.user,
            variance_amount="25.00",
            reason="Unfavorable malt price variance",
        )
        self.assertEqual(var_je.status, "posted")
        var_dr = var_je.lines.filter(account__code="5090", debit__gt=0).first()
        wip_cr = var_je.lines.filter(account__code="1220", credit__gt=0).first()
        self.assertEqual(var_dr.debit, Decimal("25.00"))
        self.assertEqual(wip_cr.credit, Decimal("25.00"))

    def test_manufacturing_accounting_idempotency(self):
        from accounting.manufacturing_accounting import post_manufacturing_accounting

        je1, created1 = post_manufacturing_accounting(self.order.id, self.company, user=self.user)
        self.assertTrue(created1)

        # Repeated post should not create another journal entry
        je2, created2 = post_manufacturing_accounting(self.order.id, self.company, user=self.user)
        self.assertFalse(created2)
        self.assertEqual(je1.id, je2.id)

    def test_manufacturing_accounting_reversal(self):
        from accounting.manufacturing_accounting import (
            post_manufacturing_accounting,
            reverse_manufacturing_accounting,
        )

        je, _ = post_manufacturing_accounting(self.order.id, self.company, user=self.user)
        self.assertEqual(je.status, "posted")

        rev_je = reverse_manufacturing_accounting(
            self.order.id,
            self.company,
            user=self.user,
            reason="Order cancelled after QA audit",
        )
        self.assertEqual(rev_je.status, "posted")
        self.assertEqual(rev_je.reversal_of, je)
        je.refresh_from_db()
        self.assertEqual(je.status, "reversed")

    def test_no_duplicate_gl_from_inventory_out_movement(self):
        from inventory.services import increase_stock, decrease_stock
        from accounting.manufacturing_accounting import post_manufacturing_accounting
        from accounting.inventory_accounting import determine_movement_accounting_requirement

        # Post manufacturing entry for order
        post_manufacturing_accounting(self.order.id, self.company, user=self.user)

        # Inventory movement created for this production order with stock available
        increase_stock(self.malt, self.warehouse, 100, user=self.user, reference="Initial Malt Stock")
        decrease_stock(self.malt, self.warehouse, 40, user=self.user, reference=f"Production #{self.order.id}")
        mov = StockMovement.objects.filter(reference=f"Production #{self.order.id}").latest("created_at")

        # Inventory requirement check detects order is already accounted in manufacturing
        req, reason, subtype = determine_movement_accounting_requirement(mov, self.company)
        self.assertFalse(req)
        self.assertEqual(subtype, "production_order")
        self.assertIn("already accounted under manufacturing", reason.lower())


class ManufacturingAccountingAPITests(APITestCase):
    """
    REST API tests for /api/accounting/manufacturing/ endpoints.
    """

    def setUp(self):
        self.company = Company.objects.create(name="BrewCraft API Corp", slug="brewcraft-api")
        ensure_account_types()
        self.user = User.objects.create_user(
            username="mfg_api_user",
            email="mfg_api@brewcraft.com",
            password="password123",
            company=self.company,
        )
        self.client.force_authenticate(user=self.user)

        seed_standard_fiscal_year(self.company, 2026)
        seed_standard_chart_of_accounts(self.company)

        self.settings = AccountingSettings.objects.get_or_create(
            company=self.company,
            defaults={"manufacturing_accounting_enabled": True, "wip_accounting_enabled": True},
        )[0]
        self.settings.manufacturing_accounting_enabled = True
        self.settings.wip_accounting_enabled = True
        self.settings.save()

        self.warehouse = Warehouse.objects.create(company=self.company, name="Central Warehouse")
        self.barley = Item.objects.create(
            name="Roasted Barley",
            sku="RAW-BARLEY",
            category="raw_material",
            purchase_cost=Decimal("5.00"),
            company=self.company,
        )
        self.stout = Item.objects.create(
            name="Imperial Stout 12pk",
            sku="FG-STOUT-12",
            category="finished_good",
            selling_price=Decimal("40.00"),
            company=self.company,
        )
        self.recipe = Recipe.objects.create(product=self.stout, batch_size=25)
        RecipeIngredient.objects.create(recipe=self.recipe, item=self.barley, quantity=10.0)

        self.order = ProductionOrder.objects.create(
            recipe=self.recipe,
            quantity=50.0,
            warehouse=self.warehouse,
            status="completed",
        )

    def test_manufacturing_summary_endpoint(self):
        res = self.client.get("/api/accounting/manufacturing/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("active_wip_balance", res.data)
        self.assertIn("finished_goods_value", res.data)
        self.assertEqual(res.data["total_orders_count"], 1)

    def test_manufacturing_orders_list_endpoint(self):
        res = self.client.get("/api/accounting/manufacturing/orders/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["count"], 1)
        first = res.data["results"][0]
        self.assertEqual(first["id"], self.order.id)
        self.assertEqual(first["product_name"], "Imperial Stout 12pk")
        self.assertEqual(first["accounting_status"], "PENDING")

    def test_manufacturing_preview_and_post_flow(self):
        # 1. Preview
        prev_res = self.client.post(f"/api/accounting/manufacturing/{self.order.id}/preview/")
        self.assertEqual(prev_res.status_code, status.HTTP_200_OK)
        self.assertTrue(prev_res.data["is_balanced"])
        self.assertEqual(prev_res.data["planned_quantity"], 50.0)

        # 2. Post
        post_res = self.client.post(f"/api/accounting/manufacturing/{self.order.id}/post/", {
            "notes": "Posted batch via API",
        })
        self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(post_res.data["status"], "posted")
        je_id = post_res.data["journal_entry_id"]

        # 3. Check updated status
        orders_res = self.client.get("/api/accounting/manufacturing/orders/")
        self.assertEqual(orders_res.status_code, status.HTTP_200_OK)
        order_data = orders_res.data["results"][0]
        self.assertEqual(order_data["accounting_status"], "POSTED")
        self.assertEqual(order_data["journal_entry_id"], je_id)

        # 4. Reverse
        rev_res = self.client.post(f"/api/accounting/manufacturing/{self.order.id}/reverse/", {
            "reason": "Test reversal API",
        })
        self.assertEqual(rev_res.status_code, status.HTTP_200_OK)
        self.assertEqual(rev_res.data["status"], "posted")


# ==============================================================================
# BLUEPRINT SECTION #16 — EXPENSES-TO-ACCOUNTING TEST SUITE
# ==============================================================================

from django.core.files.uploadedfile import SimpleUploadedFile
from workforce.models import Employee, Department
from procurement.models import Vendor
from accounting.models import Expense, ExpenseCategory, ExpenseAuditLog
from accounting.expenses_accounting import (
    seed_default_expense_categories,
    get_expenses_policy,
    resolve_expense_account,
    resolve_tax_account,
    resolve_payment_account,
    submit_expense,
    approve_expense,
    reject_expense,
    cancel_expense,
    attach_receipt_to_expense,
    get_expense_accounting_preview,
    post_expense_accounting,
    reverse_expense_accounting,
    get_expenses_summary,
)


class ExpenseAccountingTests(TestCase):
    """
    Comprehensive unit tests for Section #16 Expenses-to-Accounting subledger:
    Categories, account mapping, approval workflows, receipt handling, tax accounting,
    payment sources, double-entry GL posting, idempotency, reversal, and period controls.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Stout Craft Breweries", slug="stoutcraft")
        self.user = User.objects.create_user(
            username="finance_auditor",
            email="auditor@stoutcraft.com",
            password="testpassword123",
            company=self.company,
            role="finance",
        )
        self.settings = AccountingSettings.objects.create(
            company=self.company,
            expenses_accounting_enabled=True,
            expenses_require_approval=True,
        )
        seed_standard_chart_of_accounts(self.company)
        self.fy = seed_standard_fiscal_year(self.company, year=2026)
        self.today = date(2026, 3, 15)

        # Seed categories
        seed_default_expense_categories(self.company)
        self.travel_category = ExpenseCategory.objects.get(company=self.company, code="TRAVEL")
        self.software_category = ExpenseCategory.objects.get(company=self.company, code="SOFTWARE")
        self.office_category = ExpenseCategory.objects.get(company=self.company, code="OFFICE")

        # Create workforce employee
        self.department = Department.objects.create(company=self.company, name="Field Operations")
        self.employee = Employee.objects.create(
            company=self.company,
            first_name="Marcus",
            last_name="Vance",
            email="marcus.vance@stoutcraft.com",
            department=self.department,
        )

        # Create procurement vendor
        self.vendor = Vendor.objects.create(
            company=self.company,
            name="Apex Cloud Services LLC",
            email="billing@apexcloud.com",
        )

    def test_seed_default_categories(self):
        """Verifies default categories are provisioned and mapped to leaf accounts."""
        cats = ExpenseCategory.objects.filter(company=self.company)
        self.assertGreaterEqual(cats.count(), 10)
        travel = cats.filter(code="TRAVEL").first()
        self.assertIsNotNone(travel)
        self.assertIsNotNone(travel.expense_account)
        self.assertFalse(travel.expense_account.is_header)

    def test_employee_expense_creation_and_total_calculation(self):
        """Tests that total_amount is automatically synchronized from pre-tax + tax."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Flight to Craft Beer Expo",
            expense_date=self.today,
            amount_before_tax=Decimal("450.00"),
            tax_amount=Decimal("36.00"),
            payment_source="payable",
            created_by=self.user,
        )
        self.assertEqual(exp.total_amount, Decimal("486.00"))
        self.assertTrue(exp.expense_number.startswith("EXP-"))
        self.assertEqual(exp.approval_status, "draft")
        self.assertEqual(exp.accounting_status, "not_ready")

    def test_vendor_expense_creation(self):
        """Tests vendor expense logging with custom raw vendor fallback."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="vendor",
            vendor=self.vendor,
            category=self.software_category,
            title="Monthly ERP Server Hosting",
            expense_date=self.today,
            amount_before_tax=Decimal("1200.00"),
            tax_amount=Decimal("0.00"),
            payment_source="bank",
            created_by=self.user,
        )
        self.assertEqual(exp.total_amount, Decimal("1200.00"))
        self.assertEqual(exp.payment_source, "bank")

    def test_approval_lifecycle_enforcement(self):
        """
        Draft and submitted expenses cannot be posted when approval is required.
        Only approved expenses are eligible.
        """
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Hotel Stay",
            expense_date=self.today,
            amount_before_tax=Decimal("300.00"),
            tax_amount=Decimal("0.00"),
            payment_source="payable",
            created_by=self.user,
        )

        # 1. Draft cannot post
        with self.assertRaises(ValidationError) as ctx:
            post_expense_accounting(exp, user=self.user)
        self.assertIn("cannot be posted in 'draft'", str(ctx.exception))

        # 2. Submit for review
        submit_expense(exp, user=self.user)
        self.assertEqual(exp.approval_status, "submitted")

        # 3. Submitted cannot post
        with self.assertRaises(ValidationError) as ctx:
            post_expense_accounting(exp, user=self.user)
        self.assertIn("cannot be posted in 'submitted'", str(ctx.exception))

        # 4. Reject
        reject_expense(exp, user=self.user, reason="Missing receipt itemization")
        self.assertEqual(exp.approval_status, "rejected")
        self.assertEqual(exp.rejection_reason, "Missing receipt itemization")

        # 5. Rejected cannot post
        with self.assertRaises(ValidationError):
            post_expense_accounting(exp, user=self.user)

        # 6. Re-submit and Approve
        submit_expense(exp, user=self.user)
        approve_expense(exp, user=self.user, notes="Receipt verified")
        self.assertEqual(exp.approval_status, "approved")
        self.assertEqual(exp.approved_by, self.user)
        self.assertEqual(exp.accounting_status, "ready")

        # 7. Approved can post successfully
        je = post_expense_accounting(exp, user=self.user)
        self.assertEqual(je.status, "posted")
        self.assertEqual(exp.accounting_status, "posted")

    def test_receipt_attachment_validation(self):
        """Validates receipt file upload extension and size checks."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.office_category,
            title="Printer Paper & Ink",
            expense_date=self.today,
            amount_before_tax=Decimal("75.00"),
            payment_source="cash",
            created_by=self.user,
        )

        # Valid PDF receipt
        valid_file = SimpleUploadedFile("receipt_invoice.pdf", b"%PDF-1.4 test receipt content", content_type="application/pdf")
        attach_receipt_to_expense(exp, valid_file, user=self.user)
        self.assertTrue(bool(exp.receipt))
        self.assertEqual(exp.receipt_name, "receipt_invoice.pdf")
        self.assertGreater(exp.receipt_size, 0)

        # Audit log created
        log = ExpenseAuditLog.objects.filter(expense=exp, action="receipt_attached").first()
        self.assertIsNotNone(log)

        # Invalid file format (e.g. .exe)
        invalid_file = SimpleUploadedFile("malicious.exe", b"binary content", content_type="application/octet-stream")
        with self.assertRaises(ValidationError) as ctx:
            attach_receipt_to_expense(exp, invalid_file, user=self.user)
        self.assertIn("Unsupported file format", str(ctx.exception))

    def test_payment_source_account_resolution(self):
        """Verifies correct leaf accounts are credited for CASH, BANK, and PAYABLE."""
        exp_cash = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.office_category,
            title="Local Hardware Store Petty Cash",
            expense_date=self.today,
            amount_before_tax=Decimal("40.00"),
            payment_source="cash",
        )
        cash_acc = resolve_payment_account(exp_cash)
        self.assertEqual(cash_acc.code, "1030")

        exp_bank = Expense.objects.create(
            company=self.company,
            expense_type="vendor",
            vendor=self.vendor,
            category=self.software_category,
            title="Wire Transfer for IT Consulting",
            expense_date=self.today,
            amount_before_tax=Decimal("1500.00"),
            payment_source="bank",
        )
        bank_acc = resolve_payment_account(exp_bank)
        self.assertEqual(bank_acc.code, "1010")

        exp_emp_payable = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Mileage Reimbursement",
            expense_date=self.today,
            amount_before_tax=Decimal("120.00"),
            payment_source="payable",
        )
        emp_pay_acc = resolve_payment_account(exp_emp_payable)
        self.assertEqual(emp_pay_acc.code, "2100")

        exp_vendor_payable = Expense.objects.create(
            company=self.company,
            expense_type="vendor",
            vendor=self.vendor,
            category=self.software_category,
            title="Invoiced Software Annual License",
            expense_date=self.today,
            amount_before_tax=Decimal("5000.00"),
            payment_source="payable",
        )
        vendor_pay_acc = resolve_payment_account(exp_vendor_payable)
        self.assertEqual(vendor_pay_acc.code, "2010")

    def test_tax_accounting_and_equilibrium(self):
        """
        Verifies tax calculation and double-entry equilibrium:
        DR Expense (pre-tax) + DR Input Tax Recoverable (tax) == CR Payment Source (total)
        """
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Car Rental + VAT",
            expense_date=self.today,
            amount_before_tax=Decimal("500.00"),
            tax_amount=Decimal("90.00"),
            payment_source="bank",
            approval_status="approved",
            created_by=self.user,
        )
        self.assertEqual(exp.total_amount, Decimal("590.00"))

        # Preview check
        prev = get_expense_accounting_preview(exp)
        self.assertTrue(prev["balanced"])
        self.assertEqual(prev["total_debit"], 590.0)
        self.assertEqual(prev["total_credit"], 590.0)
        self.assertEqual(len(prev["prospective_lines"]), 3)

        # Post
        je = post_expense_accounting(exp, user=self.user)
        self.assertEqual(je.status, "posted")
        self.assertTrue(je.is_balanced)
        self.assertEqual(je.total_debit, Decimal("590.00"))
        self.assertEqual(je.total_credit, Decimal("590.00"))

        # Inspect lines
        lines = list(je.lines.all())
        self.assertEqual(len(lines), 3)
        debit_lines = [l for l in lines if l.debit > 0]
        credit_lines = [l for l in lines if l.credit > 0]
        self.assertEqual(len(debit_lines), 2)
        self.assertEqual(len(credit_lines), 1)

        # One debit is tax account 1310
        tax_line = next(l for l in debit_lines if l.account.code == "1310")
        self.assertEqual(tax_line.debit, Decimal("90.00"))

        # Credit is bank account 1010
        self.assertEqual(credit_lines[0].account.code, "1010")
        self.assertEqual(credit_lines[0].credit, Decimal("590.00"))

    def test_idempotency_prevents_duplicate_posting(self):
        """Repeated posting attempts must be rejected with explicit error and create no extra journals."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Conference Registration",
            expense_date=self.today,
            amount_before_tax=Decimal("250.00"),
            payment_source="bank",
            approval_status="approved",
            created_by=self.user,
        )

        je1 = post_expense_accounting(exp, user=self.user)
        self.assertEqual(je1.status, "posted")

        # Second attempt raises ValidationError
        with self.assertRaises(ValidationError) as ctx:
            post_expense_accounting(exp, user=self.user)
        self.assertIn("has already been posted", str(ctx.exception))

        # Only 1 journal entry exists
        count = JournalEntry.objects.filter(company=self.company, source_module="expenses", source_id=str(exp.id)).count()
        self.assertEqual(count, 1)

    def test_reversal_creates_mirrored_journal_entry(self):
        """Tests that reversing a posted expense creates a balanced reversal journal."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Train Ticket to Client",
            expense_date=self.today,
            amount_before_tax=Decimal("80.00"),
            payment_source="cash",
            approval_status="approved",
            created_by=self.user,
        )
        je = post_expense_accounting(exp, user=self.user)
        self.assertEqual(exp.accounting_status, "posted")

        rev_je = reverse_expense_accounting(exp, user=self.user, reason="Trip rescheduled")
        self.assertEqual(exp.accounting_status, "reversed")
        self.assertEqual(rev_je.status, "posted")
        self.assertEqual(rev_je.reversal_of, je)

        # Check reversal lines invert original lines
        orig_debit = je.lines.filter(debit__gt=0).first()
        orig_credit = je.lines.filter(credit__gt=0).first()

        rev_credit = rev_je.lines.filter(account=orig_debit.account).first()
        rev_debit = rev_je.lines.filter(account=orig_credit.account).first()

        self.assertEqual(rev_credit.credit, orig_debit.debit)
        self.assertEqual(rev_debit.debit, orig_credit.credit)

    def test_closed_period_and_lock_date_rejection(self):
        """Posting to a locked date or closed period must fail."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Prior Year Expense",
            expense_date=date(2025, 1, 1),
            amount_before_tax=Decimal("100.00"),
            approval_status="approved",
            created_by=self.user,
        )

        # No open period exists for 2025-01-01
        with self.assertRaises(ValidationError):
            post_expense_accounting(exp, user=self.user)

        # Lock date test
        self.settings.lock_date = date(2026, 3, 20)
        self.settings.save()

        exp_locked = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Locked Period Expense",
            expense_date=date(2026, 3, 10),
            amount_before_tax=Decimal("150.00"),
            approval_status="approved",
            created_by=self.user,
        )
        with self.assertRaises(ValidationError) as ctx:
            post_expense_accounting(exp_locked, user=self.user)
        self.assertIn("period is locked", str(ctx.exception))

    def test_company_isolation(self):
        """Ensures cross-company accounts and records are strictly rejected."""
        company_b = Company.objects.create(name="Competitor Brewing", slug="competitor")
        seed_standard_chart_of_accounts(company_b)

        foreign_cat = ExpenseCategory.objects.create(
            company=company_b,
            code="COMP_TRAVEL",
            name="Competitor Travel",
        )

        exp = Expense(
            company=self.company,
            expense_type="employee",
            category=foreign_cat,
            title="Cross Company Test",
            expense_date=self.today,
            amount_before_tax=Decimal("100.00"),
        )
        with self.assertRaises(ValidationError):
            exp.full_clean()

    def test_expenses_summary_aggregation(self):
        """Verifies get_expenses_summary calculates counts, totals, tax, and payables."""
        # 1. Draft
        Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Draft Exp",
            expense_date=self.today,
            amount_before_tax=Decimal("100.00"),
            approval_status="draft",
        )
        # 2. Submitted
        Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Submitted Exp",
            expense_date=self.today,
            amount_before_tax=Decimal("200.00"),
            approval_status="submitted",
        )
        # 3. Approved & Posted
        exp_posted = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Posted Exp",
            expense_date=self.today,
            amount_before_tax=Decimal("300.00"),
            tax_amount=Decimal("30.00"),
            payment_source="payable",
            approval_status="approved",
        )
        post_expense_accounting(exp_posted, user=self.user)

        summary = get_expenses_summary(self.company)
        self.assertEqual(summary["total_expenses_count"], 3)
        self.assertEqual(summary["pending_approval_count"], 1)
        self.assertEqual(summary["pending_approval_amount"], 200.0)
        self.assertEqual(summary["posted_count"], 1)
        self.assertEqual(summary["posted_amount"], 330.0)
        self.assertEqual(summary["posted_tax_amount"], 30.0)
        self.assertEqual(summary["employee_reimbursements_amount"], 330.0)


class ExpenseAccountingAPITests(APITestCase):
    """
    Integration tests for Section #16 REST endpoints:
    /api/accounting/expenses/ and /api/accounting/expense-categories/.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Highland Brewing", slug="highland")
        self.user = User.objects.create_user(
            username="finance_admin",
            email="finance@highland.com",
            password="testpassword123",
            company=self.company,
            role="finance",
        )
        self.client.force_authenticate(user=self.user)

        self.settings = AccountingSettings.objects.create(
            company=self.company,
            expenses_accounting_enabled=True,
            expenses_require_approval=True,
        )
        seed_standard_chart_of_accounts(self.company)
        seed_standard_fiscal_year(self.company, year=2026)
        seed_default_expense_categories(self.company)
        self.travel_category = ExpenseCategory.objects.get(company=self.company, code="TRAVEL")

        self.department = Department.objects.create(company=self.company, name="Sales")
        self.employee = Employee.objects.create(
            company=self.company,
            first_name="Sarah",
            last_name="Connor",
            email="sarah.connor@highland.com",
            department=self.department,
        )

    def test_expense_crud_api(self):
        """Tests expense creation, retrieval, and listing via REST API."""
        # Create
        res = self.client.post("/api/accounting/expenses/", {
            "expense_type": "employee",
            "employee": self.employee.id,
            "category": self.travel_category.id,
            "title": "Client Lunch in Chicago",
            "expense_date": "2026-03-15",
            "amount_before_tax": "150.00",
            "tax_amount": "15.00",
            "payment_source": "payable",
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        exp_id = res.data["id"]
        self.assertEqual(res.data["total_amount"], "165.00")

        # List
        list_res = self.client.get("/api/accounting/expenses/")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_res.data), 1)

        # Retrieve detail
        det_res = self.client.get(f"/api/accounting/expenses/{exp_id}/")
        self.assertEqual(det_res.status_code, status.HTTP_200_OK)
        self.assertIn("audit_logs", det_res.data)

    def test_expense_workflow_actions_api(self):
        """Tests submit, approve, preview, post, and reverse via REST API."""
        create_res = self.client.post("/api/accounting/expenses/", {
            "expense_type": "employee",
            "employee": self.employee.id,
            "category": self.travel_category.id,
            "title": "Taxi to Airport",
            "expense_date": "2026-03-15",
            "amount_before_tax": "60.00",
            "tax_amount": "0.00",
            "payment_source": "cash",
        })
        exp_id = create_res.data["id"]

        # 1. Submit
        sub_res = self.client.post(f"/api/accounting/expenses/{exp_id}/submit/")
        self.assertEqual(sub_res.status_code, status.HTTP_200_OK)
        self.assertEqual(sub_res.data["approval_status"], "submitted")

        # 2. Approve
        app_res = self.client.post(f"/api/accounting/expenses/{exp_id}/approve/", {"notes": "Approved by manager"})
        self.assertEqual(app_res.status_code, status.HTTP_200_OK)
        self.assertEqual(app_res.data["approval_status"], "approved")

        # 3. Preview
        prev_res = self.client.post(f"/api/accounting/expenses/{exp_id}/preview/")
        self.assertEqual(prev_res.status_code, status.HTTP_200_OK)
        self.assertTrue(prev_res.data["balanced"])
        self.assertEqual(prev_res.data["total_debit"], 60.0)

        # 4. Post
        post_res = self.client.post(f"/api/accounting/expenses/{exp_id}/post/")
        self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(post_res.data["status"], "posted")
        je_num = post_res.data["journal_entry_number"]
        self.assertTrue(bool(je_num))

        # 5. Reverse
        rev_res = self.client.post(f"/api/accounting/expenses/{exp_id}/reverse/", {"reason": "Duplicate claim"})
        self.assertEqual(rev_res.status_code, status.HTTP_200_OK)
        self.assertEqual(rev_res.data["status"], "posted")

    def test_expense_receipt_upload_api(self):
        """Tests multipart receipt file upload via REST API."""
        exp = Expense.objects.create(
            company=self.company,
            expense_type="employee",
            employee=self.employee,
            category=self.travel_category,
            title="Subway Pass",
            expense_date=date(2026, 3, 15),
            amount_before_tax=Decimal("25.00"),
        )
        sample_file = SimpleUploadedFile("subway_receipt.png", b"fake image bytes", content_type="image/png")
        res = self.client.post(
            f"/api/accounting/expenses/{exp.id}/receipt/",
            {"file": sample_file},
            format="multipart",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["receipt_name"], "subway_receipt.png")

    def test_expense_summary_api(self):
        """Tests the summary KPI endpoint."""
        res = self.client.get("/api/accounting/expenses/summary/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("total_expenses_count", res.data)
        self.assertIn("gl_operating_expense_net", res.data)

    def test_expense_categories_seed_api(self):
        """Tests the seed-defaults endpoint for expense categories."""
        res = self.client.post("/api/accounting/expense-categories/seed-defaults/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("Successfully seeded", res.data["message"])










