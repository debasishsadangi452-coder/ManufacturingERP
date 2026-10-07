"""Track B finance integration (TB-14): GL follows the business transactions."""
from decimal import Decimal

from rest_framework import status

from accounting.models import JournalEntry
from accounting.test_auto_posting import AutoPostingTestBase, lines_of
from inventory.models import Stock
from production.models import ProductionOrder
from quality.models import QualityCheck
from sales.models import SalesOrder, SalesOrderItem


class TrackBLedgerTests(AutoPostingTestBase):
    def run_post(self, url, data=None, expect=status.HTTP_200_OK):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(url, data or {}, format="json")
        self.assertEqual(res.status_code, expect, getattr(res, "data", res))
        return res

    def test_partial_qa_and_partial_dispatch_post_to_gl(self):
        Stock.objects.create(item=self.sugar, warehouse=self.warehouse, quantity=100)
        so = SalesOrder.objects.create(customer=self.customer, status="pending")
        line = SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=100, unit_price=Decimal("4.00"))
        self.run_post(f"/api/sales/sales-orders/{so.id}/mark_ready_for_production/")
        prod = ProductionOrder.objects.get(sales_order=so)

        self.run_post(f"/api/production/production-orders/{prod.id}/complete/")
        self.assertFalse(JournalEntry.objects.filter(company=self.company, source_module="manufacturing").exists(),
                         "nothing is posted while QA is pending")
        qc = QualityCheck.objects.get(production_order=prod)
        self.run_post(f"/api/quality/quality-checks/{qc.id}/decide/", {"accepted_quantity": 90, "rejected_quantity": 10})
        mfg = lines_of(self.entry("manufacturing"))
        # 100 bottles at 1.00: 90 to finished goods, 10 to scrap, WIP cleared.
        self.assertEqual(mfg["1230"][0], Decimal("90.00"))
        net_wip = mfg["1220"][0] - mfg["1220"][1]
        self.assertEqual(net_wip, Decimal("0.00"))

        self.run_post(f"/api/fulfillment/orders/{so.id}/allocate/", expect=status.HTTP_201_CREATED)
        for qty in (40, 50):
            d = self.run_post("/api/fulfillment/dispatches/", {
                "sales_order": so.id, "lines": [{"order_item_id": line.id, "quantity": qty}],
            }, status.HTTP_201_CREATED).data["id"]
            for step, data in (("pick", None), ("pack", None), ("stage", None),
                               ("prepare", {"shipment_mode": "sea", "carrier": "Maersk"}),
                               ("verify", None), ("confirm", None)):
                self.run_post(f"/api/fulfillment/dispatches/{d}/{step}/", data)

        cogs = JournalEntry.objects.filter(company=self.company, source_module="fulfillment.dispatch", status="posted")
        self.assertEqual(cogs.count(), 2, "each partial dispatch posts its own COGS")
        self.assertEqual(sorted(lines_of(e)["5050"][0] for e in cogs), [Decimal("40.00"), Decimal("50.00")])
        invoices = JournalEntry.objects.filter(company=self.company, source_module="sales.invoice", status="posted")
        self.assertEqual(sorted(lines_of(e)["1100"][0] for e in invoices), [Decimal("160.00"), Decimal("200.00")])
        so.refresh_from_db()
        self.assertEqual(so.status, "shipped")  # 10 rejected units still owed

        trace = self.client.get(f"/api/insights/traceability/?type=sales_order&value={so.id}").data
        gl = [s for s in trace["results"][0]["stages"] if s["key"] == "gl"][0]
        self.assertEqual(gl["count"], 5)  # manufacturing + 2 COGS + 2 invoices

    def test_rework_rejection_and_incoming_rejection_are_written_off(self):
        from procurement.models import PurchaseOrder, PurchaseOrderItem
        from production.models import ManufacturingSettings, ScrapRecord
        Stock.objects.create(item=self.sugar, warehouse=self.warehouse, quantity=100)
        order = ProductionOrder.objects.create(recipe=self.recipe, quantity=100, warehouse=self.warehouse)
        self.run_post(f"/api/production/production-orders/{order.id}/complete/")
        qc = QualityCheck.objects.get(production_order=order)
        self.run_post(f"/api/quality/quality-checks/{qc.id}/decide/", {"accepted_quantity": 80, "rework_quantity": 20})
        rework_id = self.run_post(f"/api/quality/quality-checks/{qc.id}/send_to_rework/", expect=status.HTTP_201_CREATED).data["id"]
        self.run_post(f"/api/production/production-orders/{rework_id}/complete/")
        rqc = QualityCheck.objects.get(production_order_id=rework_id)
        self.run_post(f"/api/quality/quality-checks/{rqc.id}/decide/", {"accepted_quantity": 15, "rejected_quantity": 5})
        scrap = ScrapRecord.objects.get(production_order_id=rework_id)
        self.assertEqual((scrap.gl_treatment, scrap.cost_impact), ("finished_goods", Decimal("5.00")))
        entry = lines_of(self.entry("production.scrap"))
        self.assertEqual(entry["1230"][1], Decimal("5.00"))
        self.assertFalse(JournalEntry.objects.filter(company=self.company, source_module="manufacturing",
                                                     source_id=rework_id).exists())

        ManufacturingSettings.objects.update_or_create(company=self.company, defaults={"incoming_qc_required": True})
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="approved")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.sugar, quantity=50, unit_price=Decimal("2.00"))
        self.run_post("/api/procurement/goods-receipts/", {"purchase_order": po.id, "warehouse": self.warehouse.id},
                      expect=status.HTTP_201_CREATED)
        iqc = QualityCheck.objects.get(inspection_type="incoming")
        self.run_post(f"/api/quality/quality-checks/{iqc.id}/decide/", {"accepted_quantity": 40, "rejected_quantity": 10})
        rej = lines_of(self.entry("quality.incoming_rejection"))
        self.assertEqual((rej["2050"][0], rej["1210"][1]), (Decimal("20.00"), Decimal("20.00")))
