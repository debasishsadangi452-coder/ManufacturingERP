"""Setting finished-good selling prices from the Sales price list."""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from inventory.models import Item
from production.models import Recipe, RecipeIngredient

URL = "/api/sales/price-list/"


class PriceListTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fizz Works", slug="fizzworks")
        self.user = User.objects.create_user(
            username="sam.sales", email="sam@fizz.test", role="sales", company=self.company, password="pass",
        )
        sugar = Item.objects.create(company=self.company, name="Sugar", category="raw_material",
                                    purchase_cost=Decimal("2.00"))
        self.soda = Item.objects.create(company=self.company, name="Soda", category="finished_good",
                                        selling_price=Decimal("5.00"))
        recipe = Recipe.objects.create(product=self.soda, batch_size=10)
        RecipeIngredient.objects.create(recipe=recipe, item=sugar, quantity=5)
        self.client.force_authenticate(user=self.user)

    def test_lists_finished_goods_with_price_and_standard_cost(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)  # raw materials are not on the price list
        self.assertEqual(res.data[0]["selling_price"], "5.00")
        self.assertEqual(res.data[0]["standard_cost"], "1.00")

    def test_sales_user_updates_selling_price(self):
        res = self.client.patch(f"{URL}{self.soda.id}/", {"selling_price": "6.5"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.soda.refresh_from_db()
        self.assertEqual(self.soda.selling_price, Decimal("6.50"))

    def test_rejects_negative_or_foreign_items(self):
        self.assertEqual(self.client.patch(f"{URL}{self.soda.id}/", {"selling_price": "-1"}, format="json").status_code,
                         status.HTTP_400_BAD_REQUEST)
        other = Company.objects.create(name="Other", slug="other")
        foreign = Item.objects.create(company=other, name="Cola", category="finished_good")
        self.assertEqual(self.client.patch(f"{URL}{foreign.id}/", {"selling_price": "1"}, format="json").status_code,
                         status.HTTP_404_NOT_FOUND)
