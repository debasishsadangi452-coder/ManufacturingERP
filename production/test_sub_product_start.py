"""Starting a needed sub-product from the shop floor, once."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import Item, Warehouse
from production.execution import create_production_order
from production.models import ProductionOrder, Recipe, RecipeIngredient
from production.wip import wip_row


class StartSubProductTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Sub Co")
        self.user = User.objects.create_user(username="p@sub", role="production", company=self.company, password="p")
        self.wh = Warehouse.objects.create(company=self.company, name="Plant", location="x", warehouse_type="production")
        flour = Item.objects.create(company=self.company, name="Flour", category="raw_material", unit="kg")
        self.dough = Item.objects.create(company=self.company, name="Dough", category="intermediate", unit="kg")
        self.cookie = Item.objects.create(company=self.company, name="Cookie", category="finished_good", unit="each")
        dough_recipe = Recipe.objects.create(product=self.dough, batch_size=1)
        RecipeIngredient.objects.create(recipe=dough_recipe, item=flour, quantity=1)
        cookie_recipe = Recipe.objects.create(product=self.cookie, batch_size=1)
        RecipeIngredient.objects.create(recipe=cookie_recipe, item=self.dough, quantity=2)
        self.order, _ = create_production_order(cookie_recipe, 5, self.wh, validate=False)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/production/production-orders/{self.order.id}/start_sub_product/"

    def test_row_flags_sub_product_without_a_work_order(self):
        line = next(l for l in wip_row(self.order)["material_lines"] if l["item_id"] == self.dough.id)
        self.assertTrue(line["is_sub_product"])
        self.assertIsNone(line["sub_work_order"])

    def test_starts_it_once_for_the_missing_quantity(self):
        res = self.client.post(self.url, {"item": self.dough.id}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        sub = ProductionOrder.objects.get(recipe__product=self.dough)
        self.assertEqual(sub.quantity, 10)  # 5 cookies x 2 dough each
        line = next(l for l in wip_row(self.order)["material_lines"] if l["item_id"] == self.dough.id)
        self.assertEqual(line["sub_work_order"]["id"], sub.id)
        again = self.client.post(self.url, {"item": self.dough.id}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(ProductionOrder.objects.filter(recipe__product=self.dough).count(), 1)

    def test_rejects_non_sub_products(self):
        flour = Item.objects.get(name="Flour")
        self.assertEqual(self.client.post(self.url, {"item": flour.id}, format="json").status_code, 400)
