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


class MaterialLineAccuracyTests(TestCase):
    def setUp(self):
        from inventory.models import UnitOfMeasure
        self.company = Company.objects.create(name="Units Co")
        self.wh = Warehouse.objects.create(company=self.company, name="Plant", location="x", warehouse_type="production")
        self.kg = UnitOfMeasure.objects.create(company=self.company, code="kg", name="Kilogram", dimension="mass", is_base=True)
        self.tonne = UnitOfMeasure.objects.create(company=self.company, code="t", name="Tonne", dimension="mass",
                                                  to_base_factor=1000)
        self.fg = Item.objects.create(company=self.company, name="Cookie", category="finished_good", unit="each")
        self.raw = Item.objects.create(company=self.company, name="Colour", category="raw_material", unit="kg", base_unit=self.kg)
        recipe = Recipe.objects.create(product=self.fg, batch_size=1)
        RecipeIngredient.objects.create(recipe=recipe, item=self.raw, quantity=1)
        self.order, _ = create_production_order(recipe, 10, self.wh, validate=False)

    def test_float_dust_is_not_a_shortage(self):
        from inventory.models import Stock
        from production.execution import material_lines
        Stock.objects.create(item=self.raw, warehouse=self.wh, quantity=10 - 1e-9)
        self.assertEqual(material_lines(self.order)[0]["shortage"], 0)

    def test_on_order_is_reported_in_stock_unit(self):
        from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor
        from production.execution import material_lines
        vendor = Vendor.objects.create(company=self.company, name="V")
        po = PurchaseOrder.objects.create(vendor=vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.raw, quantity=0.002, unit_of_measure=self.tonne,
                                         unit_price=1)
        self.assertAlmostEqual(material_lines(self.order)[0]["on_order"], 2.0, places=3)  # 0.002 t = 2 kg
