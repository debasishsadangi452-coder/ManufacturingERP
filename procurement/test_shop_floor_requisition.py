from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import Item, Warehouse
from production.execution import create_production_order
from production.models import Recipe, RecipeIngredient
from procurement.models import PurchaseRequisition

URL = "/api/procurement/requisitions/create_from_production_order/"


class ShopFloorRequisitionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Req Co")
        self.user = User.objects.create_user(username="p@req", role="production", company=self.company, password="p")
        wh = Warehouse.objects.create(company=self.company, name="Plant", location="x", warehouse_type="production")
        fg = Item.objects.create(company=self.company, name="Cookie", category="finished_good", unit="each")
        self.flour = Item.objects.create(company=self.company, name="Flour", category="raw_material", unit="kg")
        recipe = Recipe.objects.create(product=fg, batch_size=1)
        RecipeIngredient.objects.create(recipe=recipe, item=self.flour, quantity=2)
        self.order, _ = create_production_order(recipe, 10, wh, validate=False)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_raises_a_requisition_for_the_shortage_once(self):
        res = self.client.post(URL, {"production_order": self.order.id}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        req = PurchaseRequisition.objects.get()
        self.assertEqual(req.status, "submitted")
        line = req.items.get()
        self.assertEqual((line.item_id, line.shortage_quantity), (self.flour.id, 20))
        again = self.client.post(URL, {"production_order": self.order.id}, format="json")
        self.assertEqual((again.status_code, again.data["created"]), (200, False))
        self.assertEqual(PurchaseRequisition.objects.count(), 1)

    def test_other_companys_order_is_not_found(self):
        other = User.objects.create_user(username="x@o", role="production", password="p",
                                         company=Company.objects.create(name="Other Co"))
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(URL, {"production_order": self.order.id}, format="json").status_code, 404)
