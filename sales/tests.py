from django.test import TestCase

from accounts.models import Company, User
from inventory.models import Item

from .models import Customer, SalesOrder
from .serializers import SalesOrderSerializer


class SerializerRequest:
    def __init__(self, data, user):
        self.data = data
        self.user = user


class SalesOrderItemValidationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Demo Plant")
        self.user = User.objects.create_user(
            username="sales_user",
            password="pass123",
            role="sales",
            company=self.company,
        )
        self.customer = Customer.objects.create(company=self.company, name="Retail Chain")
        self.raw_item = Item.objects.create(
            company=self.company,
            name="Cane Sugar",
            category="raw_material",
            unit="kg",
        )
        self.finished_item = Item.objects.create(
            company=self.company,
            name="Sparkling Water",
            category="finished_good",
            unit="bottle",
            selling_price="20.00",
        )

    def _request(self, item):
        return SerializerRequest(
            {"items": [{"item": item.id, "quantity": 5}]},
            self.user,
        )

    def test_sales_order_rejects_raw_materials(self):
        serializer = SalesOrderSerializer(
            data={"customer": self.customer.id},
            context={"request": self._request(self.raw_item)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        with self.assertRaisesMessage(Exception, "Only finished goods can be sold"):
            serializer.save()

        self.assertFalse(SalesOrder.objects.exists())

    def test_sales_order_accepts_finished_goods(self):
        serializer = SalesOrderSerializer(
            data={"customer": self.customer.id},
            context={"request": self._request(self.finished_item)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        order = serializer.save()

        self.assertEqual(order.salesorderitem_set.count(), 1)
        self.assertEqual(order.total_amount, 100)
