"""Assigning ingredients to vendors from the Procurement catalogue."""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item
from procurement.models import Vendor, VendorPriceList

URL = "/api/procurement/vendor-prices/"


class VendorAssignmentTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
        self.other = Company.objects.create(name="Other Co", slug="otherco")
        self.user = User.objects.create_user(
            username="sid.store", email="sid@fizz.test", role="store", company=self.company, password="pass",
        )
        self.sugar = Item.objects.create(company=self.company, name="Sugar", category="raw_material")
        self.vendor = Vendor.objects.create(company=self.company, name="Sweet Supplies")
        self.client.force_authenticate(user=self.user)

    def test_assign_ingredient_to_vendor_and_update_price(self):
        res = self.client.post(URL, {
            "vendor": self.vendor.id, "item": self.sugar.id, "unit_price": "1.90",
            "min_order_qty": 50, "lead_time_days": 5,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data["vendor_name"], "Sweet Supplies")

        res = self.client.patch(f"{URL}{res.data['id']}/", {"unit_price": "1.75"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(VendorPriceList.objects.get().unit_price, Decimal("1.75"))
        self.assertEqual(len(self.client.get(URL, {"item": self.sugar.id}).data), 1)

    def test_cannot_assign_another_companys_vendor_or_item(self):
        foreign_vendor = Vendor.objects.create(company=self.other, name="Foreign Vendor")
        foreign_item = Item.objects.create(company=self.other, name="Foreign Sugar", category="raw_material")

        res = self.client.post(URL, {"vendor": foreign_vendor.id, "item": self.sugar.id, "unit_price": "1"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vendor", res.data)

        res = self.client.post(URL, {"vendor": self.vendor.id, "item": foreign_item.id, "unit_price": "1"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("item", res.data)
        self.assertFalse(VendorPriceList.objects.exists())
