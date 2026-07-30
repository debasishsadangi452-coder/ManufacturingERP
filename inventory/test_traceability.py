"""P1 SQF traceability: the receive → produce → QC → ship lot chain.

These exercise the lot lifecycle service directly (not the HTTP views) to prove
the genealogy links hold both directions: a shipped finished lot traces back to
the raw lots consumed, and a raw lot traces forward to the customer it reached.
"""

from django.test import TestCase

from accounts.models import Company
from inventory.models import Batch, LotConsumption, Item, Warehouse
from inventory.lots import (
    create_raw_lot, create_finished_lot, consume_lots_fifo, ship_lots_fifo,
    trace_backward, trace_forward,
)
from procurement.models import GoodsReceipt, PurchaseOrder, Vendor
from production.models import ProductionOrder, Recipe
from sales.models import Customer, SalesOrder, Shipment, ShipmentLot


class LotTraceabilityChainTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Red Velvet NYC")
        self.wh = Warehouse.objects.create(company=self.company, name="Plant", location="Mount Kisco NY")
        self.sugar = Item.objects.create(company=self.company, name="Cane Sugar", category="raw_material", unit="g")
        self.cookie = Item.objects.create(company=self.company, name="Cookies & Cream", category="finished_good", unit="each")

        self.vendor = Vendor.objects.create(company=self.company, name="Sugar Co")
        self.po = PurchaseOrder.objects.create(vendor=self.vendor, status="approved")
        self.receipt = GoodsReceipt.objects.create(purchase_order=self.po, warehouse=self.wh)

    def test_raw_lot_created_at_receipt(self):
        lot = create_raw_lot(self.sugar, self.wh, 1000, self.receipt, company=self.company)
        self.assertEqual(lot.source, "received")
        self.assertEqual(lot.remaining_quantity, 1000)
        self.assertEqual(lot.goods_receipt, self.receipt)

    def test_production_consumes_raw_lots_fifo(self):
        # Two raw lots; production draws 1200 → all of lot A + 200 of lot B.
        lot_a = create_raw_lot(self.sugar, self.wh, 1000, self.receipt, company=self.company, lot_code="RM-A")
        lot_b = create_raw_lot(self.sugar, self.wh, 500, self.receipt, company=self.company, lot_code="RM-B")
        recipe = Recipe.objects.create(product=self.cookie)
        po = ProductionOrder.objects.create(recipe=recipe, quantity=100, warehouse=self.wh)

        consumptions = consume_lots_fifo(po, self.sugar, 1200, company=self.company)

        self.assertEqual(len(consumptions), 2)
        lot_a.refresh_from_db(); lot_b.refresh_from_db()
        self.assertEqual(lot_a.remaining_quantity, 0)
        self.assertEqual(lot_b.remaining_quantity, 300)

    def test_full_chain_backward_and_forward(self):
        # Receive raw → produce finished (consuming raw) → ship finished.
        raw = create_raw_lot(self.sugar, self.wh, 1000, self.receipt, company=self.company, lot_code="RM-SUGAR-1")
        recipe = Recipe.objects.create(product=self.cookie)
        po = ProductionOrder.objects.create(recipe=recipe, quantity=200, warehouse=self.wh)
        consume_lots_fifo(po, self.sugar, 800, company=self.company)
        finished = create_finished_lot(self.cookie, self.wh, 200, po, company=self.company, lot_code="FG-CC-1")

        customer = Customer.objects.create(company=self.company, name="Costco NE")
        order = SalesOrder.objects.create(customer=customer, status="confirmed")
        shipment = Shipment.objects.create(sales_order=order, warehouse=self.wh)
        ship_lots_fifo(shipment, self.cookie, 200, company=self.company)

        # Backward: finished lot → the raw sugar lot it consumed.
        back = trace_backward(finished)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["lot_code"], "RM-SUGAR-1")
        self.assertEqual(back[0]["quantity_consumed"], 800)

        # Forward: raw lot → finished lot → customer shipment.
        fwd = trace_forward(raw)
        self.assertEqual(len(fwd), 1)
        self.assertEqual(fwd[0]["finished_lot"], "FG-CC-1")
        self.assertEqual(fwd[0]["shipments"][0]["customer"], "Costco NE")
        self.assertEqual(fwd[0]["shipments"][0]["quantity"], 200)

    def test_shipment_lot_link_recorded(self):
        finished = create_finished_lot(self.cookie, self.wh, 50, None, company=self.company, lot_code="FG-2")
        customer = Customer.objects.create(company=self.company, name="FreshMart")
        order = SalesOrder.objects.create(customer=customer, status="confirmed")
        shipment = Shipment.objects.create(sales_order=order, warehouse=self.wh)

        ship_lots_fifo(shipment, self.cookie, 50, company=self.company)

        link = ShipmentLot.objects.get(shipment=shipment)
        self.assertEqual(link.lot, finished)
        self.assertEqual(link.quantity, 50)
        finished.refresh_from_db()
        self.assertEqual(finished.remaining_quantity, 0)

    def test_consumption_is_best_effort_when_no_lots(self):
        # Legacy stock with no lots: production shouldn't crash, just records nothing.
        recipe = Recipe.objects.create(product=self.cookie)
        po = ProductionOrder.objects.create(recipe=recipe, quantity=10, warehouse=self.wh)
        consumptions = consume_lots_fifo(po, self.sugar, 500, company=self.company)
        self.assertEqual(consumptions, [])


class P2OperationsTests(TestCase):
    """P2: cases→batches planning, inter-warehouse transfers, cycle counting."""

    def setUp(self):
        from inventory.services import increase_stock
        self.company = Company.objects.create(name="Bakery Co")
        self.plant = Warehouse.objects.create(company=self.company, name="Plant", location="Mount Kisco")
        self.milton = Warehouse.objects.create(company=self.company, name="Milton", location="Milton NY")
        self.cookie = Item.objects.create(company=self.company, name="Cookies & Cream", category="finished_good", unit="each")
        increase_stock(self.cookie, self.plant, 500, reference="seed")

    def test_cases_to_batches_rounds_up_with_overrun(self):
        recipe = Recipe.objects.create(product=self.cookie, batch_size=48)
        plan = recipe.batches_for(200)  # 200 cases, 48/batch → 5 batches (240), overrun 40
        self.assertEqual(plan["batches"], 5)
        self.assertEqual(plan["units_produced"], 240)
        self.assertEqual(plan["overrun"], 40)

    def test_exact_multiple_has_no_overrun(self):
        recipe = Recipe.objects.create(product=self.cookie, batch_size=50)
        self.assertEqual(recipe.batches_for(150)["batches"], 3)
        self.assertEqual(recipe.batches_for(150)["overrun"], 0)

    def test_transfer_moves_stock_between_warehouses(self):
        from inventory.models import Stock, StockTransfer
        from inventory.operations import complete_transfer
        t = StockTransfer.objects.create(
            company=self.company, item=self.cookie,
            source_warehouse=self.plant, dest_warehouse=self.milton, quantity=200,
        )
        complete_transfer(t)
        self.assertEqual(Stock.objects.get(item=self.cookie, warehouse=self.plant).quantity, 300)
        self.assertEqual(Stock.objects.get(item=self.cookie, warehouse=self.milton).quantity, 200)
        t.refresh_from_db()
        self.assertEqual(t.status, "completed")

    def test_transfer_cannot_complete_twice(self):
        from inventory.models import StockTransfer
        from inventory.operations import complete_transfer
        from rest_framework.exceptions import ValidationError
        t = StockTransfer.objects.create(
            company=self.company, item=self.cookie,
            source_warehouse=self.plant, dest_warehouse=self.milton, quantity=100,
        )
        complete_transfer(t)
        with self.assertRaises(ValidationError):
            complete_transfer(t)

    def test_cycle_count_posts_variance_as_adjustment(self):
        from inventory.models import Stock, CycleCount, CycleCountLine
        from inventory.operations import post_cycle_count
        # System says 500; physical count finds 480 → post should set stock to 480.
        count = CycleCount.objects.create(company=self.company, warehouse=self.plant)
        CycleCountLine.objects.create(
            cycle_count=count, item=self.cookie, system_quantity=500, counted_quantity=480,
        )
        adjusted = post_cycle_count(count)
        self.assertEqual(adjusted, 1)
        self.assertEqual(Stock.objects.get(item=self.cookie, warehouse=self.plant).quantity, 480)
        count.refresh_from_db()
        self.assertEqual(count.status, "posted")

    def test_cycle_count_no_variance_no_adjustment(self):
        from inventory.models import CycleCount, CycleCountLine
        from inventory.operations import post_cycle_count
        count = CycleCount.objects.create(company=self.company, warehouse=self.plant)
        CycleCountLine.objects.create(
            cycle_count=count, item=self.cookie, system_quantity=500, counted_quantity=500,
        )
        self.assertEqual(post_cycle_count(count), 0)
