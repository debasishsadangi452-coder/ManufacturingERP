"""New work orders are given the next free slot instead of staying "not scheduled"."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import Item, Warehouse
from production.execution import create_production_order
from production.models import ProductionOrder, Recipe, RecipeIngredient
from production.wip import wip_row


class AutoScheduleTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Auto Sched Co")
        self.user = User.objects.create_user(
            username="pat.production@autosched", email="pat@a.test", role="production",
            company=self.company, password="pass",
        )
        self.warehouse = Warehouse.objects.create(company=self.company, name="Plant", location="X", warehouse_type="production")
        finished = Item.objects.create(company=self.company, name="Cookie", category="finished_good", unit="each")
        raw = Item.objects.create(company=self.company, name="Flour", category="raw_material", unit="kg")
        self.recipe = Recipe.objects.create(product=finished, batch_size=1)
        RecipeIngredient.objects.create(recipe=self.recipe, item=raw, quantity=1)

    def _make(self, **kw):
        order, _ = create_production_order(self.recipe, 100, self.warehouse, validate=False, **kw)
        order.refresh_from_db()
        return order

    def test_new_order_gets_a_planned_start(self):
        order = self._make()
        self.assertIsNotNone(order.planned_start)
        self.assertGreaterEqual(order.planned_start, timezone.now() - timedelta(seconds=5))
        self.assertTrue(wip_row(order)["is_scheduled"])

    def test_explicit_planned_start_is_kept(self):
        when = timezone.now() + timedelta(days=3)
        self.assertEqual(self._make(planned_start=when).planned_start, when)

    def test_schedule_unscheduled_places_orders_without_a_start(self):
        order = self._make()
        ProductionOrder.objects.filter(pk=order.pk).update(planned_start=None, planned_end=None)
        self.assertFalse(wip_row(ProductionOrder.objects.get(pk=order.pk))["is_scheduled"])
        client = APIClient()
        client.force_authenticate(user=self.user)
        res = client.post("/api/production/production-orders/schedule_unscheduled/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["scheduled"], [order.order_number])
        order.refresh_from_db()
        self.assertIsNotNone(order.planned_start)

    def test_wip_row_exposes_created_at(self):
        self.assertIn("created_at", wip_row(self._make()))


class CostTrendTests(TestCase):
    def test_invoice_dated_in_a_future_month_does_not_crash(self):
        from datetime import date, timedelta
        from decimal import Decimal
        from production.costing import monthly_cost_vs_sales
        from sales.models import Customer, Invoice
        company = Company.objects.create(name="Trend Co")
        customer = Customer.objects.create(company=company, name="C")
        Invoice.objects.create(company=company, customer=customer, total_amount=Decimal("10"),
                               invoice_date=date.today() + timedelta(days=45))
        self.assertEqual(len(monthly_cost_vs_sales(company, 6)), 6)
