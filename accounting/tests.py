from django.test import TestCase
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient
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
