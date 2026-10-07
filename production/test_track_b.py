"""Track B acceptance scenarios (spec section 36) and integrity rules (section 31)."""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Company, User
from fulfillment.models import Dispatch, FGAllocation
from inventory.models import Batch, Item, Stock, Warehouse
from production.models import (
    ManufacturingSettings, ProductionMaterialRequirement, ProductionOperation, ProductionOrder,
    Recipe, RecipeIngredient, Resource, RoutingStep, ScrapRecord,
)
from quality.models import QualityCheck
from sales.models import Customer, InvoiceLine, SalesOrder, SalesOrderItem


class TrackBBase(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="VGT Plant", slug="vgt")
        self.user = User.objects.create_user(username="admin.vgt", password="pass123", role="admin", company=self.company)
        self.client.force_authenticate(self.user)
        self.wh = Warehouse.objects.create(company=self.company, name="Main", location="Plant")
        self.customer = Customer.objects.create(company=self.company, name="Acme Foods", address="1 Road")
        self.raw = Item.objects.create(company=self.company, name="Resin", category="raw_material", unit="kg",
                                       purchase_cost=Decimal("2.00"))
        self.fg = Item.objects.create(company=self.company, name="Widget", category="finished_good", unit="ea",
                                      selling_price=Decimal("500.00"))
        self.recipe = Recipe.objects.create(product=self.fg, batch_size=1)
        RecipeIngredient.objects.create(recipe=self.recipe, item=self.raw, quantity=1)

    # helpers -------------------------------------------------------------
    def stock(self, item, qty):
        Stock.objects.update_or_create(item=item, warehouse=self.wh, defaults={"quantity": qty})

    def qty(self, item):
        s = Stock.objects.filter(item=item).first()
        return s.quantity if s else 0

    def order(self, qty, price=None):
        so = SalesOrder.objects.create(customer=self.customer, status="pending")
        SalesOrderItem.objects.create(sales_order=so, item=self.fg, quantity=qty,
                                      unit_price=price if price is not None else Decimal("0"))
        return so

    def post(self, url, data=None, expect=status.HTTP_200_OK):
        res = self.client.post(url, data or {}, format="json")
        self.assertEqual(res.status_code, expect, getattr(res, "data", res))
        return res

    def release(self, so):
        self.post(f"/api/sales/sales-orders/{so.id}/mark_ready_for_production/")
        return ProductionOrder.objects.get(sales_order=so, rework_of__isnull=True)

    def output(self, prod, qty, expect=status.HTTP_200_OK):
        return self.post(f"/api/production/production-orders/{prod.id}/report_output/", {"quantity": qty}, expect)

    def decide(self, qc, **q):
        return self.post(f"/api/quality/quality-checks/{qc.id}/decide/", q)

    def dispatch_all(self, so, lines=None):
        res = self.post("/api/fulfillment/dispatches/", {"sales_order": so.id, "lines": lines}, status.HTTP_201_CREATED)
        d = res.data["id"]
        base = f"/api/fulfillment/dispatches/{d}"
        self.post(f"{base}/pick/")
        self.post(f"{base}/pack/", {"packages": 2})
        self.post(f"{base}/stage/")
        self.post(f"{base}/prepare/", {"shipment_mode": "road", "carrier": "BlueDart", "shipment_reference": "LR-1"})
        self.post(f"{base}/verify/")
        self.post(f"{base}/confirm/")
        return Dispatch.objects.get(pk=d)


class Scenario1CompleteMTO(TrackBBase):
    def test_order_to_invoice_stays_linked(self):
        self.stock(self.raw, 1000)
        so = self.order(1000, Decimal("500"))
        prod = self.release(so)
        self.assertEqual(prod.status, "scheduled")
        self.assertTrue(prod.order_number.startswith("PRD-"))
        self.assertEqual(prod.operations.count(), 1)

        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        prod.refresh_from_db()
        self.assertEqual((prod.status, prod.produced_quantity, prod.qa_status), ("completed", 1000, "pending"))
        self.assertEqual(self.qty(self.fg), 0, "FG must not be sellable before QA")

        qc = QualityCheck.objects.get(production_order=prod)
        self.decide(qc, accepted_quantity=1000)
        self.assertEqual(self.qty(self.fg), 1000)
        prod.refresh_from_db()
        self.assertEqual(prod.qa_status, "passed")

        res = self.post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        self.assertEqual(res.data["lines"][0]["allocated"], 1000)
        d = self.dispatch_all(so)
        so.refresh_from_db()
        self.assertEqual(so.status, "delivered")
        self.assertEqual(d.status, "dispatched")
        self.assertIsNotNone(d.invoice_id)
        line = InvoiceLine.objects.get(invoice=d.invoice)
        self.assertEqual((line.quantity, line.unit_price), (1000, Decimal("500.00")))
        self.assertEqual(self.qty(self.fg), 0)
        self.assertTrue(d.shipment.shipment_lots.exists(), "dispatched lots are recorded for genealogy")

        trace = self.client.get(f"/api/insights/traceability/?type=sales_order&value=SO-{so.id}").data
        stages = {s["key"]: s for s in trace["results"][0]["stages"]}
        for key in ("production_order", "material_issue", "wip", "qa", "fg", "allocation", "picking", "dispatch", "invoice", "ar"):
            self.assertTrue(stages[key]["complete"], key)
        # every search entry point resolves to the same order
        for t, v in (("production_order", prod.order_number), ("dispatch", d.dispatch_number),
                     ("invoice", f"INV-{d.invoice_id}"), ("lot", qc.lot.batch_number)):
            res = self.client.get(f"/api/insights/traceability/?type={t}&value={v}").data
            self.assertEqual(res["results"][0]["customer_order"]["id"], so.id, t)


class Scenario2PartialMaterial(TrackBBase):
    def test_reserved_material_is_never_deducted_twice(self):
        RecipeIngredient.objects.filter(recipe=self.recipe).update(quantity=100)
        self.stock(self.raw, 60)
        so = self.order(1)
        prod = self.release(so)
        self.assertEqual(prod.status, "material_pending")
        ledger = ProductionMaterialRequirement.objects.get(production_order=prod)
        self.assertEqual((ledger.required_quantity, ledger.consumed_quantity, ledger.shortage_quantity), (100, 60, 40))
        self.assertEqual(self.qty(self.raw), 0)

        # Completion with no further stock fails cleanly; nothing changes.
        res = self.client.post(f"/api/production/production-orders/{prod.id}/complete/")
        self.assertEqual(res.status_code, 400)
        ledger.refresh_from_db()
        self.assertEqual(ledger.consumed_quantity, 60)

        self.stock(self.raw, 45)
        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        ledger.refresh_from_db()
        self.assertEqual((ledger.consumed_quantity, ledger.shortage_quantity), (100, 0))
        self.assertEqual(self.qty(self.raw), 5, "only the 40 still required is drawn")


class Scenario3QAFailure(TrackBBase):
    def test_failed_units_are_never_sellable(self):
        self.stock(self.raw, 1000)
        so = self.order(1000)
        prod = self.release(so)
        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        qc = QualityCheck.objects.get(production_order=prod)
        self.decide(qc, accepted_quantity=800, rejected_quantity=200)
        self.assertEqual(self.qty(self.fg), 800)
        rejected = Batch.objects.get(parent=qc.lot, qa_status="rejected")
        self.assertEqual(rejected.remaining_quantity, 200)
        prod.refresh_from_db()
        self.assertEqual(prod.qa_status, "partial")

        res = self.post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        self.assertEqual(res.data["lines"][0]["allocated"], 800)
        self.assertIn("awaits production", " ".join(res.data["messages"]))
        res = self.client.post("/api/fulfillment/dispatches/", {"sales_order": so.id, "lines": [
            {"order_item_id": so.salesorderitem_set.first().id, "quantity": 1000}]}, format="json")
        self.assertEqual(res.status_code, 400)
        inv = {r["item"]: r for r in self.client.get("/api/fulfillment/fg-inventory/").data}["Widget"]
        self.assertEqual((inv["qa_approved"], inv["qa_rejected"], inv["available"], inv["allocated"]), (800, 200, 0, 800))


class Scenario4AgreedPrice(TrackBBase):
    def test_invoice_uses_order_price_not_item_master(self):
        res = self.client.post("/api/sales/sales-orders/", {
            "customer": self.customer.id, "items": [{"item": self.fg.id, "quantity": 100, "unit_price": "500"}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        so = SalesOrder.objects.get(pk=res.data["id"])
        self.fg.selling_price = Decimal("550")
        self.fg.save()
        self.stock(self.raw, 100)
        prod = self.release(so)
        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        self.decide(QualityCheck.objects.get(production_order=prod), accepted_quantity=100)
        self.post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        d = self.dispatch_all(so)
        line = InvoiceLine.objects.get(invoice=d.invoice)
        self.assertEqual(line.unit_price, Decimal("500.00"))
        self.assertEqual(d.invoice.total_amount, Decimal("50000.00"))


class Scenario5PartialProduction(TrackBBase):
    def test_partial_outputs_keep_remaining(self):
        self.stock(self.raw, 1000)
        so = self.order(1000)
        prod = self.release(so)
        self.output(prod, 600)
        prod.refresh_from_db()
        self.assertEqual((prod.produced_quantity, prod.remaining_quantity, prod.status), (600, 400, "partially_completed"))
        self.output(prod, 500, expect=400)  # cannot over-produce
        self.output(prod, 400)
        prod.refresh_from_db()
        self.assertEqual((prod.produced_quantity, prod.status), (1000, "completed"))
        self.assertEqual(QualityCheck.objects.filter(production_order=prod).count(), 2)
        so.refresh_from_db()
        self.assertEqual(so.status, "confirmed", "order is not fulfilled by production alone")

    def test_short_close_and_material_proportional_issue(self):
        RecipeIngredient.objects.filter(recipe=self.recipe).update(quantity=2)
        self.stock(self.raw, 0)
        prod = ProductionOrder.objects.create(recipe=self.recipe, quantity=10, warehouse=self.wh)
        self.stock(self.raw, 20)
        self.output(prod, 4)
        ledger = ProductionMaterialRequirement.objects.get(production_order=prod)
        self.assertAlmostEqual(ledger.consumed_quantity, 8)
        self.decide(QualityCheck.objects.get(production_order=prod), accepted_quantity=4)
        self.post(f"/api/production/production-orders/{prod.id}/close/")
        prod.refresh_from_db()
        self.assertEqual((prod.status, prod.produced_quantity), ("closed", 4))


class Scenario6PartialDispatch(TrackBBase):
    def test_partial_dispatch_and_invoice(self):
        self.stock(self.raw, 1000)
        so = self.order(1000, Decimal("10"))
        prod = self.release(so)
        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        self.decide(QualityCheck.objects.get(production_order=prod), accepted_quantity=1000)
        self.post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        oi = so.salesorderitem_set.first()
        d1 = self.dispatch_all(so, [{"order_item_id": oi.id, "quantity": 400}])
        so.refresh_from_db()
        oi.refresh_from_db()
        self.assertEqual((so.status, oi.shipped_quantity), ("shipped", 400))
        self.assertEqual(d1.invoice.total_amount, Decimal("4000.00"))
        d2 = self.dispatch_all(so, [{"order_item_id": oi.id, "quantity": 300}])
        d3 = self.dispatch_all(so)
        so.refresh_from_db()
        self.assertEqual(so.status, "delivered")
        self.assertEqual(sum(InvoiceLine.objects.filter(sales_order_item=oi).values_list("quantity", flat=True)), 1000)
        self.assertEqual({d1.dispatch_number, d2.dispatch_number, d3.dispatch_number}.__len__(), 3)
        res = self.client.post(f"/api/fulfillment/dispatches/{d3.id}/invoice/")
        self.assertEqual(res.status_code, 201)  # already invoiced: returns the same invoice
        self.assertEqual(res.data["id"], d3.invoice_id)
        res = self.client.post(f"/api/sales/sales-orders/{so.id}/generate_invoice/")
        self.assertEqual(res.status_code, 400, "nothing left to invoice")


class Scenario7Rework(TrackBBase):
    def test_rework_keeps_original_order_and_lot(self):
        self.stock(self.raw, 100)
        so = self.order(100)
        prod = self.release(so)
        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        qc = QualityCheck.objects.get(production_order=prod)
        self.decide(qc, accepted_quantity=70, rework_quantity=30)
        self.assertEqual(self.qty(self.fg), 70)
        res = self.post(f"/api/quality/quality-checks/{qc.id}/send_to_rework/", expect=status.HTTP_201_CREATED)
        rework = ProductionOrder.objects.get(pk=res.data["id"])
        self.assertEqual((rework.rework_of_id, rework.sales_order_id, rework.quantity), (prod.id, so.id, 30))
        raw_before = self.qty(self.raw)
        self.post(f"/api/production/production-orders/{rework.id}/complete/")
        self.assertEqual(self.qty(self.raw), raw_before, "rework consumes no new material")
        rqc = QualityCheck.objects.get(production_order=rework)
        self.assertEqual(rqc.lot.parent.parent_id, qc.lot_id)
        self.decide(rqc, accepted_quantity=30)
        self.assertEqual(self.qty(self.fg), 100)
        trace = self.client.get(f"/api/insights/traceability/?type=production_order&value={rework.order_number}").data
        orders = [r["label"] for s in trace["results"][0]["stages"] if s["key"] == "production_order" for r in s["records"]]
        self.assertEqual(len(orders), 2)


class OperationsAndResources(TrackBBase):
    def setUp(self):
        super().setUp()
        self.machine = Resource.objects.create(company=self.company, name="Press 1", resource_type="machine",
                                               cost_per_hour=Decimal("60"), hours_per_shift=8, shifts_per_day=1)
        self.crew = Resource.objects.create(company=self.company, name="Crew A", resource_type="manpower",
                                            cost_per_hour=Decimal("30"))
        for seq, name in ((1, "Mixing"), (2, "Moulding"), (3, "Assembly")):
            RoutingStep.objects.create(recipe=self.recipe, sequence=seq, name=name, machine=self.machine,
                                       manpower=self.crew, run_minutes_per_unit=1)
        self.stock(self.raw, 100)
        self.prod = ProductionOrder.objects.create(recipe=self.recipe, quantity=100, warehouse=self.wh)
        from production.execution import build_operations
        self.ops = build_operations(self.prod)

    def report(self, op, expect=200, **q):
        return self.post(f"/api/production/operations/{op.id}/report/", q, expect)

    def test_sequence_enforced_and_wip_board(self):
        op1, op2, op3 = self.ops
        self.report(op2, good=10, expect=400)  # nothing has finished op 1 yet
        self.report(op1, good=60)
        self.report(op2, good=70, expect=400)  # only 60 reached op 2
        self.report(op2, good=50, scrap=5)
        self.post(f"/api/production/operations/{op3.id}/report/", {"good": 1, "complete": True}, 400)
        self.assertEqual(ScrapRecord.objects.filter(production_order=self.prod).count(), 1)

        board = {r["id"]: r for r in self.client.get("/api/production/wip/").data}[self.prod.id]
        self.assertEqual(board["current_operation"]["name"], "Mixing")
        self.assertEqual(board["scrap_quantity"], 5)
        self.assertEqual(board["bottleneck"]["operation"], "Op 3 Assembly")

        self.report(op3, good=50)
        self.output(self.prod, 60, expect=400)  # only 50 finished the final op
        self.output(self.prod, 50)

    def test_costing_and_capacity(self):
        cost = self.client.get(f"/api/production/production-orders/{self.prod.id}/costing/").data
        # planned: material 100 kg x 2 = 200; 300 minutes = 5 h x (60 + 30) = 450
        self.assertEqual(cost["planned"]["raw_material_cost"], 200.0)
        self.assertEqual(cost["planned"]["resource_cost"], 450.0)
        self.assertEqual(cost["planned"]["total_cost"], 650.0)
        self.assertTrue(cost["selling_price_excluded"])
        plan = self.client.get("/api/production/capacity/?days=1").data
        press = [m for m in plan["machines"] if m["name"] == "Press 1"][0]
        self.assertEqual(press["available_hours"], round(8 * 6 / 7, 2))
        self.assertEqual(press["load_hours"], 5.0)
        self.assertEqual(press["utilisation_percent"], 72.9)
        self.assertFalse(press["overloaded"])
        # A second order on the same press pushes it past its available hours.
        second = ProductionOrder.objects.create(recipe=self.recipe, quantity=100, warehouse=self.wh)
        from production.execution import build_operations
        build_operations(second)
        press = [m for m in self.client.get("/api/production/capacity/?days=1").data["machines"] if m["name"] == "Press 1"][0]
        self.assertTrue(press["overloaded"])
        self.assertGreater(press["expected_delay_days"], 0)
        check = self.client.get(f"/api/production/production-orders/{second.id}/capacity_check/").data
        self.assertTrue(check["ok"] or check["issues"])


class AllocationIntegrity(TrackBBase):
    def test_same_fg_never_allocated_twice(self):
        self.stock(self.fg, 100)
        a, b = self.order(80), self.order(80)
        for so in (a, b):
            so.status = "confirmed"
            so.save()
        self.post(f"/api/fulfillment/orders/{a.id}/allocate/", expect=status.HTTP_201_CREATED)
        res = self.post(f"/api/fulfillment/orders/{b.id}/allocate/", expect=status.HTTP_201_CREATED)
        self.assertEqual(res.data["lines"][0]["allocated"], 20)
        total = sum(FGAllocation.objects.filter(status="active").values_list("quantity", flat=True))
        self.assertEqual(total, 100)
        # legacy direct fulfilment of B cannot take A's allocated stock
        res = self.client.post(f"/api/sales/sales-orders/{b.id}/fulfill_order/")
        self.assertEqual(res.status_code, 400)

    def test_dispatch_stage_order_enforced(self):
        self.stock(self.fg, 10)
        so = self.order(10)
        so.status = "confirmed"
        so.save()
        self.post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        d = self.post("/api/fulfillment/dispatches/", {"sales_order": so.id}, status.HTTP_201_CREATED).data["id"]
        self.post(f"/api/fulfillment/dispatches/{d}/confirm/", expect=400)
        self.post(f"/api/fulfillment/dispatches/{d}/pick/")
        self.post(f"/api/fulfillment/dispatches/{d}/pack/")
        self.post(f"/api/fulfillment/dispatches/{d}/stage/")
        self.post(f"/api/fulfillment/dispatches/{d}/prepare/", {"shipment_mode": "air"}, 400)  # carrier required
        self.post(f"/api/fulfillment/dispatches/{d}/prepare/", {"shipment_mode": "air", "carrier": "DHL"})


class IncomingQC(TrackBBase):
    def test_received_material_held_until_accepted(self):
        from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor
        ManufacturingSettings.objects.create(company=self.company, incoming_qc_required=True)
        vendor = Vendor.objects.create(company=self.company, name="Polymers Ltd")
        po = PurchaseOrder.objects.create(vendor=vendor, status="approved")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.raw, quantity=50, unit_price=Decimal("2"))
        res = self.client.post("/api/procurement/goods-receipts/", {"purchase_order": po.id, "warehouse": self.wh.id}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.qty(self.raw), 0)
        qc = QualityCheck.objects.get(inspection_type="incoming")
        self.decide(qc, accepted_quantity=40, quarantined_quantity=10)
        self.assertEqual(self.qty(self.raw), 40)
        self.assertTrue(Batch.objects.filter(parent=qc.lot, qa_status="quarantine", remaining_quantity=10).exists())
        listed = self.client.get("/api/quality/quality-checks/?inspection_type=incoming").data
        self.assertEqual(len(listed), 1)


class Documents(TrackBBase):
    def test_documents_render_with_references(self):
        self.stock(self.raw, 10)
        so = self.order(10, Decimal("5"))
        prod = self.release(so)
        self.post(f"/api/production/production-orders/{prod.id}/complete/")
        qc = QualityCheck.objects.get(production_order=prod)
        self.decide(qc, accepted_quantity=10)
        self.post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        d = self.dispatch_all(so)
        for doc, pk in (("production_order", prod.id), ("traveler", prod.id), ("pick_list", prod.id),
                        ("material_requisition", prod.id), ("qa_inspection", qc.id), ("qa_certificate", qc.id),
                        ("delivery_note", d.id), ("dispatch_note", d.id), ("invoice", d.invoice_id)):
            res = self.client.get(f"/api/documents/{doc}/{pk}/")
            self.assertEqual(res.status_code, 200, doc)
            self.assertIn(f"SO-{so.id}", res.content.decode(), doc)
        other = Company.objects.create(name="Other", slug="other")
        outsider = User.objects.create_user(username="x.other", password="p", role="admin", company=other)
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(f"/api/documents/invoice/{d.invoice_id}/").status_code, 404)


class ExecutiveDashboard(TrackBBase):
    def test_dashboard_and_kpis(self):
        self.stock(self.raw, 10)
        so = self.order(10)
        prod = self.release(so)
        self.output(prod, 4)
        data = self.client.get("/api/insights/executive/").data
        self.assertEqual(data["production"]["partially_completed"], 1)
        self.assertEqual(data["production"]["production_completion_percent"], 40.0)
        self.assertEqual(data["inventory"]["fg_qa_pending"], 4)
        keys = {k["key"] for k in self.client.get("/api/insights/kpi-definitions/").data}
        self.assertIn("order_fulfilment_percent", keys)
        self.assertEqual(self.client.get("/api/insights/accounting-mapping/").status_code, 200)


class ResourceAvailability(TrackBBase):
    def test_unavailable_resources_cannot_be_assigned_or_started(self):
        from datetime import timedelta
        from django.utils import timezone
        from production.models import ResourceUnavailability
        press = Resource.objects.create(company=self.company, name="Press 9", resource_type="machine")
        crew = Resource.objects.create(company=self.company, name="Crew Z", resource_type="manpower")
        RoutingStep.objects.create(recipe=self.recipe, sequence=1, name="Press", machine=press, manpower=crew)
        prod = ProductionOrder.objects.create(recipe=self.recipe, quantity=5, warehouse=self.wh)
        from production.execution import build_operations
        op = build_operations(prod)[0]

        press.status = "maintenance"
        press.save()
        self.post(f"/api/production/operations/{op.id}/start/", expect=400)
        res = self.client.patch(f"/api/production/operations/{op.id}/", {"machine": press.id}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.patch(f"/api/production/operations/{op.id}/", {"machine": crew.id}, format="json")
        self.assertEqual(res.status_code, 400, "a manpower resource cannot be the machine")

        press.status = "available"
        press.save()
        now = timezone.now()
        ResourceUnavailability.objects.create(resource=crew, start=now - timedelta(hours=1), end=now + timedelta(hours=1), reason="Leave")
        res = self.client.post(f"/api/production/operations/{op.id}/start/")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Leave", res.data["error"])
        ResourceUnavailability.objects.all().delete()
        self.post(f"/api/production/operations/{op.id}/start/")


class FiniteCapacityScheduling(TrackBBase):
    def setUp(self):
        super().setUp()
        from datetime import datetime
        from django.utils import timezone
        ManufacturingSettings.objects.update_or_create(company=self.company, defaults={
            "default_hours_per_shift": 8, "default_shifts_per_day": 1, "default_working_days_per_week": 7,
        })
        self.press = Resource.objects.create(company=self.company, name="Press", resource_type="machine")
        for seq in (1, 2, 3):
            RoutingStep.objects.create(recipe=self.recipe, sequence=seq, name=f"Step {seq}", machine=self.press,
                                       run_minutes_per_unit=1)
        self.t0 = timezone.make_aware(datetime(2026, 10, 12, 8, 0))

    def at(self, hours):
        from datetime import timedelta
        return self.t0 + timedelta(hours=hours)

    def make(self):
        from production.execution import build_operations
        order = ProductionOrder.objects.create(recipe=self.recipe, quantity=100, warehouse=self.wh)
        build_operations(order)
        return order

    def test_orders_queue_on_shared_machine_and_skip_unavailability(self):
        from datetime import timedelta
        from production.models import ResourceUnavailability
        from production.scheduling import schedule_all
        a, b = self.make(), self.make()
        schedule_all(self.company, start=self.t0)
        ops_a = list(a.operations.order_by("sequence"))
        self.assertEqual((ops_a[0].planned_start, ops_a[2].planned_end), (self.t0, self.at(5)))
        ops_b = list(b.operations.order_by("sequence"))
        self.assertEqual(ops_b[0].planned_start, self.at(5), "B waits for A on the shared press")
        # 5 h left in the 8 h shift, B needs 5 h: ends 16:00 → wraps to next day 10:00.
        self.assertEqual(ops_b[2].planned_end, self.at(24 + 2))
        b.refresh_from_db()
        self.assertEqual(b.planned_end, self.at(26))

        ProductionOrder.objects.filter(pk=b.pk).update(status="cancelled")  # free the press again
        ResourceUnavailability.objects.create(resource=self.press, start=self.at(1), end=self.at(3), reason="Maintenance")
        res = self.client.post(f"/api/production/production-orders/{a.id}/schedule/",
                               {"start": self.t0.isoformat(), "dry_run": True}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        ops = res.data["operations"]
        # 1 h before the stop, the rest after it: the order finishes 2 h later.
        self.assertEqual(ops[-1]["planned_end"], self.at(5) + timedelta(hours=2))
        a.refresh_from_db()
        self.assertEqual(a.planned_end, self.at(5), "dry run does not save")
        self.assertEqual(self.client.post("/api/production/schedule/", {"dry_run": True}, format="json").status_code, 200)
