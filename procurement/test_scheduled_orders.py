"""Scheduled purchase orders: placed automatically on their date."""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from ai_assistant.agent_tools import schedule_procurement
from inventory.models import Item
from procurement.models import PurchaseOrder, ScheduledPurchaseOrder, Vendor, VendorEmail, VendorPriceList
from procurement.scheduled import run_due_scheduled_orders

URL = "/api/procurement/scheduled-orders/"


class ScheduledOrderTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
        self.admin = User.objects.create_user(
            username="ada.admin", email="ada@fizz.test", role="admin", company=self.company, password="pass",
        )
        self.store = User.objects.create_user(
            username="sid.store", email="sid@fizz.test", role="store", company=self.company, password="pass",
        )
        self.sugar = Item.objects.create(company=self.company, name="Sugar", category="raw_material", unit="kg")
        self.cheap = Vendor.objects.create(company=self.company, name="Cheap Sugar Co", email="orders@cheap.test")
        self.dear = Vendor.objects.create(company=self.company, name="Dear Sugar Co", email="orders@dear.test")
        VendorPriceList.objects.create(vendor=self.cheap, item=self.sugar, unit_price=Decimal("1.50"))
        VendorPriceList.objects.create(vendor=self.dear, item=self.sugar, unit_price=Decimal("2.00"))
        self.today = timezone.localdate()
        self.client.force_authenticate(user=self.admin)

    def schedule(self, **overrides):
        data = {"item": self.sugar.id, "quantity": 100, "scheduled_date": str(self.today + timedelta(days=3))}
        data.update(overrides)
        res = self.client.post(URL, data, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        return ScheduledPurchaseOrder.objects.get(pk=res.data["id"])

    def test_order_is_placed_on_its_date_not_before(self):
        schedule = self.schedule()
        self.assertEqual(run_due_scheduled_orders(self.today), [])
        self.assertFalse(PurchaseOrder.objects.exists())

        run_due_scheduled_orders(self.today + timedelta(days=3))
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, "placed")
        po = schedule.purchase_order
        self.assertEqual((po.vendor, po.status, po.total_amount), (self.cheap, "ordered", Decimal("150.00")))
        self.assertTrue(VendorEmail.objects.filter(purchase_orders=po).exists())  # vendor email drafted

    def test_chosen_vendor_is_used(self):
        self.schedule(vendor=self.dear.id, scheduled_date=str(self.today))
        run_due_scheduled_orders(self.today)
        self.assertEqual(PurchaseOrder.objects.get().vendor, self.dear)

    def test_running_twice_never_places_twice(self):
        self.schedule(scheduled_date=str(self.today))
        run_due_scheduled_orders(self.today)
        run_due_scheduled_orders(self.today)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_scheduler_without_approval_rights_sends_po_for_approval(self):
        self.client.force_authenticate(user=self.store)  # no auto-approve limit
        schedule = self.schedule(scheduled_date=str(self.today))
        run_due_scheduled_orders(self.today)
        schedule.refresh_from_db()
        self.assertEqual(schedule.purchase_order.status, "pending")
        self.assertIn("approval", schedule.last_message)

    def test_monthly_schedule_rolls_forward_and_catches_up_once(self):
        start = date(2026, 1, 31)
        schedule = ScheduledPurchaseOrder.objects.create(
            company=self.company, item=self.sugar, quantity=10, scheduled_date=start,
            repeat="monthly", created_by=self.admin,
        )
        run_due_scheduled_orders(date(2026, 4, 10))  # server was down for months
        schedule.refresh_from_db()
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual((schedule.status, schedule.scheduled_date), ("scheduled", date(2026, 4, 30)))

    def test_missing_vendor_price_fails_then_place_now_succeeds(self):
        salt = Item.objects.create(company=self.company, name="Salt", category="raw_material")
        schedule = self.schedule(item=salt.id, scheduled_date=str(self.today))
        run_due_scheduled_orders(self.today)
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, "failed")
        self.assertIn("No vendor price", schedule.last_message)

        VendorPriceList.objects.create(vendor=self.cheap, item=salt, unit_price=Decimal("0.40"))
        res = self.client.post(f"{URL}{schedule.id}/place_now/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(res.data["status"], "placed")

    def test_validation_and_cancel(self):
        res = self.client.post(URL, {"item": self.sugar.id, "quantity": 5,
                                     "scheduled_date": str(self.today - timedelta(days=1))}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        other = Company.objects.create(name="Other", slug="other")
        foreign = Item.objects.create(company=other, name="Foreign", category="raw_material")
        res = self.client.post(URL, {"item": foreign.id, "quantity": 5,
                                     "scheduled_date": str(self.today)}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        schedule = self.schedule()
        res = self.client.delete(f"{URL}{schedule.id}/")
        self.assertEqual(res.data["status"], "cancelled")
        run_due_scheduled_orders(self.today + timedelta(days=30))
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_ai_procurement_tool_schedules_an_order(self):
        when = self.today + timedelta(days=7)
        result = json.loads(schedule_procurement(self.admin, "sugar", 250, str(when), repeat="weekly"))
        self.assertIn("PURCHASE ORDER SCHEDULED", result["template"])
        schedule = ScheduledPurchaseOrder.objects.get()
        self.assertEqual((schedule.quantity, schedule.scheduled_date, schedule.repeat), (250, when, "weekly"))

        past = json.loads(schedule_procurement(self.admin, "sugar", 1, str(self.today - timedelta(days=1))))
        self.assertIn("in the past", past["template"])
