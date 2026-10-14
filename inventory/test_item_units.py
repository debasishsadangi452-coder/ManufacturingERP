from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item, UnitOfMeasure


class ItemUnitTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Unit Co", slug="unit-co")
        self.user = User.objects.create_user(username="admin.unit", password="x", role="admin", company=self.company)
        self.client.force_authenticate(self.user)
        self.kg = UnitOfMeasure.objects.get(company=None, code="kg")
        self.lb = UnitOfMeasure.objects.get(company=None, code="lb")
        self.item = Item.objects.create(company=self.company, name="Flour", category="raw_material", unit="kg",
                                        base_unit=self.kg, purchase_unit=self.kg)

    def test_stocked_unit_is_fixed_once_set(self):
        res = self.client.patch(f"/api/inventory/items/{self.item.id}/", {"base_unit": self.lb.id}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        res = self.client.patch(f"/api/inventory/items/{self.item.id}/", {"base_unit": self.kg.id, "sku": "F-1"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)

    def test_price_list_unit_is_defined_once(self):
        fg = Item.objects.create(company=self.company, name="Cookie", category="finished_good", unit="Units",
                                 selling_price=Decimal("2.00"))
        each = UnitOfMeasure.objects.get(company=None, code="each")
        res = self.client.post(f"/api/sales/price-list/{fg.id}/set_unit/", {"unit": each.id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["dimension"], "count")
        again = self.client.post(f"/api/sales/price-list/{fg.id}/set_unit/", {"unit": self.kg.id}, format="json")
        self.assertEqual(again.status_code, 400)
