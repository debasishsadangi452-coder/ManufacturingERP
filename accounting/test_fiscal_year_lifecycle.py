"""Fiscal year lifecycle: current year by default, one open year, next year only after closing."""
from datetime import date

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User

from .models import AccountingPeriod, AccountingSettings, FiscalYear
from .seeds import ensure_account_types, start_next_fiscal_year
from django.utils import timezone

URL = "/api/accounting/fiscal-years/"


class FiscalYearLifecycleTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Year Co", slug="yearco")
        self.user = User.objects.create_user(
            username="fin.yearco", email="fin@yearco.test", role="finance",
            company=self.company, password="pass",
        )
        ensure_account_types()
        self.client.force_authenticate(user=self.user)
        self.this_year = timezone.localdate().year

    def test_current_year_is_opened_by_default(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        fy = res.data[0]
        self.assertEqual(fy["name"], f"FY {self.this_year}")
        self.assertEqual(fy["start_date"], f"{self.this_year}-01-01")
        self.assertFalse(fy["is_closed"])
        self.assertEqual(fy["periods_count"], 12)
        self.assertEqual(AccountingSettings.objects.get(company=self.company).current_fiscal_year_id, fy["id"])

        # Listing again does not create another year.
        self.assertEqual(len(self.client.get(URL).data), 1)

    def test_next_year_is_blocked_while_current_year_is_open(self):
        self.client.get(URL)
        res = self.client.post(f"{URL}start_next_year/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Close it before", res.data["error"])

        res = self.client.post(URL, {
            "name": "Sneaky", "start_date": f"{self.this_year + 1}-01-01", "end_date": f"{self.this_year + 1}-12-31",
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FiscalYear.objects.filter(company=self.company).count(), 1)

    def test_closing_the_year_allows_starting_the_next_one(self):
        current = self.client.get(URL).data[0]
        # A year can only be closed once every month in it is closed.
        res = self.client.post(f"{URL}{current['id']}/close_year/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        AccountingPeriod.objects.filter(fiscal_year_id=current["id"]).update(status="closed")
        res = self.client.post(f"{URL}{current['id']}/close_year/", {"generate_closing_entry": False}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        res = self.client.post(f"{URL}start_next_year/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        nxt = self.this_year + 1
        self.assertEqual(res.data["name"], f"FY {nxt}")
        self.assertEqual(res.data["start_date"], f"{nxt}-01-01")
        self.assertEqual(res.data["end_date"], f"{nxt}-12-31")
        self.assertEqual(AccountingPeriod.objects.filter(fiscal_year_id=res.data["id"]).count(), 12)
        self.assertEqual(AccountingSettings.objects.get(company=self.company).current_fiscal_year_id, res.data["id"])

        # Only one year can be open: the previous one can't be reopened now.
        res = self.client.post(f"{URL}{current['id']}/reopen_year/", {"reason": "Late supplier invoice"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(f"FY {nxt} is open", res.data["error"])

    def test_next_year_follows_a_non_calendar_fiscal_year(self):
        fy = FiscalYear.objects.create(company=self.company, name="FY 2025-2026",
                                       start_date=date(2025, 4, 1), end_date=date(2026, 3, 31), is_closed=True)
        nxt = start_next_fiscal_year(self.company)
        self.assertEqual((nxt.name, nxt.start_date, nxt.end_date), ("FY 2026-2027", date(2026, 4, 1), date(2027, 3, 31)))
        self.assertEqual(nxt.periods.count(), 12)
        self.assertEqual(nxt.periods.order_by("period_number").first().name, "Apr 2026")
        self.assertNotEqual(fy.pk, nxt.pk)

    def test_first_year_can_still_be_created_with_custom_dates(self):
        res = self.client.post(URL, {"name": "FY 2026-2027", "start_date": "2026-04-01", "end_date": "2027-03-31"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
