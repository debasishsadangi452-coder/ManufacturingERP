"""Seed a demo baking company ('Red Velvet NYC') that exercises the P0/P1/P2
features so the new screens have real data.

    python manage.py seed_bakery_demo

Login after:  the admin username is printed / password 'demo12345'.
Re-runnable: it tears down any prior demo company first.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Company, CompanySubscription, User, generate_username
from inventory.models import (
    Item, Warehouse, UnitOfMeasure, BOM, BOMLine, StockTransfer, CycleCount, CycleCountLine, Batch,
)
from inventory.services import increase_stock, decrease_stock
from inventory.lots import create_raw_lot, create_finished_lot, consume_lots_fifo, ship_lots_fifo
from inventory.operations import complete_transfer
from procurement.models import Vendor, PurchaseOrder, PurchaseOrderItem, GoodsReceipt
from production.models import Recipe, RecipeIngredient, ProductionOrder
from sales.models import Customer, SalesOrder, SalesOrderItem, Shipment, InboundOrderEmail, ShipmentLot

SLUG = "redvelvetnyc"


class Command(BaseCommand):
    help = "Seed the Red Velvet NYC bakery demo company with P0/P1/P2 data."

    @transaction.atomic
    def handle(self, *args, **options):
        # Teardown: ShipmentLot.lot is PROTECT, so clear those first.
        old = Company.objects.filter(slug=SLUG).first()
        if old:
            ShipmentLot.objects.filter(lot__company=old).delete()
            Batch.objects.filter(company=old).delete()
            old.delete()

        company = Company.objects.create(name="Red Velvet NYC", slug=SLUG)
        CompanySubscription.objects.create(
            company=company, plan="premium_ai", status="active", onboarding_completed=True
        )
        admin = User.objects.create_user(
            username=generate_username("Owner", "admin", company),
            password="demo12345", role="admin", company=company,
            first_name="Owner", is_staff=True,
        )

        g = UnitOfMeasure.objects.get(company=None, code="g")
        oz = UnitOfMeasure.objects.get(company=None, code="oz")
        lb = UnitOfMeasure.objects.get(company=None, code="lb")

        plant = Warehouse.objects.create(company=company, name="Mount Kisco Plant", location="Mount Kisco NY")
        milton = Warehouse.objects.create(company=company, name="Milton Staging", location="Milton NY")
        Warehouse.objects.create(company=company, name="Upstate Cold Storage", location="Upstate NY")

        sugar = Item.objects.create(company=company, name="Cane Sugar", category="raw_material", unit="lb", base_unit=g, purchase_unit=lb, purchase_cost=Decimal("1.20"))
        choc = Item.objects.create(company=company, name="Chocolate Chips", category="raw_material", unit="oz", base_unit=g, purchase_unit=lb, purchase_cost=Decimal("3.50"))
        flour = Item.objects.create(company=company, name="Flour", category="raw_material", unit="lb", base_unit=g, purchase_unit=lb, purchase_cost=Decimal("0.60"))
        # Deliberately unmapped unit → appears on the Unit Setup review screen.
        vanilla = Item.objects.create(company=company, name="Vanilla Extract", category="raw_material", unit="bottle", purchase_cost=Decimal("8.00"))

        cookie = Item.objects.create(company=company, name="Cookies & Cream", category="finished_good", unit="each", selling_price=Decimal("24.00"))
        Item.objects.create(company=company, name="Chocolate Peanut Butter", category="finished_good", unit="each", selling_price=Decimal("26.00"))

        bom = BOM.objects.create(finished_good=cookie)
        BOMLine.objects.create(bom=bom, raw_material=sugar, quantity=8, unit="oz", unit_of_measure=oz)
        BOMLine.objects.create(bom=bom, raw_material=choc, quantity=6, unit="oz", unit_of_measure=oz)
        BOMLine.objects.create(bom=bom, raw_material=flour, quantity=1, unit="lb", unit_of_measure=lb)

        recipe = Recipe.objects.create(product=cookie, batch_size=48)
        RecipeIngredient.objects.create(recipe=recipe, item=sugar, quantity=227)
        RecipeIngredient.objects.create(recipe=recipe, item=choc, quantity=170)
        RecipeIngredient.objects.create(recipe=recipe, item=flour, quantity=454)

        # Receive → raw lots
        vendor = Vendor.objects.create(company=company, name="Sweet Supply Co", email="sales@sweetsupply.com")
        po = PurchaseOrder.objects.create(vendor=vendor, status="approved")
        PurchaseOrderItem.objects.create(purchase_order=po, item=sugar, quantity=200000, unit_price=Decimal("0.0012"))
        PurchaseOrderItem.objects.create(purchase_order=po, item=choc, quantity=150000, unit_price=Decimal("0.0035"))
        receipt = GoodsReceipt.objects.create(purchase_order=po, warehouse=plant)
        for poi in po.items.all():
            increase_stock(poi.item, plant, poi.quantity, user=admin, reference=f"GRN PO#{po.id}")
            create_raw_lot(poi.item, plant, poi.quantity, receipt, company=company)
        increase_stock(flour, plant, 200000, user=admin, reference="Opening stock")
        create_raw_lot(flour, plant, 200000, receipt, company=company)
        po.status = "received"; po.save()

        # Produce → finished lot (consumes raw lots)
        prod = ProductionOrder.objects.create(recipe=recipe, quantity=240, warehouse=plant, status="running")
        for ing, required in recipe.material_requirements(240):
            consume_lots_fifo(prod, ing.item, required, company=company)
        increase_stock(cookie, plant, 240, user=admin, reference=f"Production #{prod.id}")
        finished_lot = create_finished_lot(cookie, plant, 240, prod, company=company)
        prod.status = "completed"; prod.save()

        # Transfer 200 plant → Milton (leaves 40 at plant)
        transfer = StockTransfer.objects.create(
            company=company, item=cookie, source_warehouse=plant, dest_warehouse=milton,
            quantity=200, created_by=admin,
        )
        complete_transfer(transfer, user=admin)

        # Ship 150 from Milton to Costco (closes the lot chain)
        costco = Customer.objects.create(company=company, name="Costco NE", email="buyer@costco.com")
        Customer.objects.create(company=company, name="FreshMart", email="orders@freshmart.com")
        order = SalesOrder.objects.create(customer=costco, status="confirmed")
        SalesOrderItem.objects.create(sales_order=order, item=cookie, quantity=150)
        shipment = Shipment.objects.create(sales_order=order, warehouse=milton)
        decrease_stock(cookie, milton, 150, user=admin, reference=f"Shipment SO#{order.id}")
        ship_lots_fifo(shipment, cookie, 150, company=company)

        # Inbound email orders
        InboundOrderEmail.objects.create(
            company=company, sender="buyer@costco.com", subject="PO — Fall order",
            raw_body="Please prep 200 cases Cookies & Cream, 300 cases Choc PB, pickup 9/15.",
            parsed_data={"customer": "Costco NE", "pickup_date": "2026-09-15",
                         "lines": [{"product": "Cookies & Cream", "cases": 200},
                                   {"product": "Chocolate Peanut Butter", "cases": 300}],
                         "confidence": 0.97},
            confidence=0.97, status="parsed",
        )
        InboundOrderEmail.objects.create(
            company=company, sender="jane@sweettooth.co", subject="order?",
            raw_body="hey can we get some cookies, maybe 50ish? thx",
            parsed_data={"customer": "Sweet Tooth", "lines": [{"product": "Cookies & Cream", "cases": 50}],
                         "confidence": 0.55},
            confidence=0.55, status="needs_attention",
            error_message="Quantity ambiguous ('50ish'); customer not matched.",
        )

        # Open cycle count with a variance (plant has 40 left; count finds 38)
        cc = CycleCount.objects.create(company=company, warehouse=plant, created_by=admin)
        CycleCountLine.objects.create(cycle_count=cc, item=cookie, system_quantity=40, counted_quantity=38)

        self.stdout.write(self.style.SUCCESS("=== Red Velvet NYC seeded ==="))
        self.stdout.write(f"ADMIN LOGIN: {admin.username}  /  demo12345")
        self.stdout.write(f"Finished lot {finished_lot.batch_number} traces to the Costco shipment")
        self.stdout.write(f"Unmapped-unit item for Unit Setup: {vanilla.name} ('{vanilla.unit}')")
        self.stdout.write("Inbound emails: 2 · Cycle count: 1 open with variance · Transfer: plant→Milton")
