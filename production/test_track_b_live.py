"""Track B end-to-end over real HTTP (live server, JWT login, real commits).

Drives the whole MTO lifecycle the way the frontend does — customer order →
release → routing operations → partial output → partial QA with rework →
allocation → pick/pack/stage/prepare/verify/dispatch (two partial dispatches) →
invoices → payment → GL — then checks the traceability chain, the dashboard
and every printable document.
"""
import json
import urllib.error
import urllib.request
from decimal import Decimal

from django.test import LiveServerTestCase
from django.utils import timezone

from accounting.models import JournalEntry
from accounting.seeds import seed_standard_chart_of_accounts, seed_standard_fiscal_year
from accounts.models import Company, User
from inventory.models import Item, Stock, Warehouse
from production.models import Recipe, RecipeIngredient, Resource, RoutingStep
from sales.models import Customer


class TrackBLiveFlow(LiveServerTestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Live Plant", slug="liveplant")
        User.objects.create_user(username="ops.admin@liveplant", email="ops@liveplant.test",
                                 password="Secret#123", role="admin", company=self.company)
        seed_standard_chart_of_accounts(self.company)
        seed_standard_fiscal_year(self.company, timezone.localdate().year)
        self.wh = Warehouse.objects.create(company=self.company, name="Plant", location="Hall 1")
        self.customer = Customer.objects.create(company=self.company, name="Acme Retail", address="5 Market St")
        self.resin = Item.objects.create(company=self.company, name="Resin", category="raw_material", unit="kg",
                                         purchase_cost=Decimal("2.00"))
        self.widget = Item.objects.create(company=self.company, name="Widget", category="finished_good", unit="ea",
                                          selling_price=Decimal("9.00"))
        recipe = Recipe.objects.create(product=self.widget, batch_size=1)
        RecipeIngredient.objects.create(recipe=recipe, item=self.resin, quantity=1)
        press = Resource.objects.create(company=self.company, name="Press", resource_type="machine",
                                        cost_per_hour=Decimal("60"))
        crew = Resource.objects.create(company=self.company, name="Crew", resource_type="manpower",
                                       cost_per_hour=Decimal("30"))
        RoutingStep.objects.create(recipe=recipe, sequence=1, name="Moulding", machine=press, manpower=crew,
                                   run_minutes_per_unit=0.5)
        RoutingStep.objects.create(recipe=recipe, sequence=2, name="Packing", manpower=crew, run_minutes_per_unit=0.2)
        Stock.objects.create(item=self.resin, warehouse=self.wh, quantity=100)
        self.token = self.call("POST", "/api/token/", {"username": "ops@liveplant.test", "password": "Secret#123"},
                               auth=False)["access"]

    def call(self, method, path, data=None, auth=True, expect=None, raw=False):
        req = urllib.request.Request(self.live_server_url + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req) as res:
                status, body = res.status, res.read().decode()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode()
        if expect is not None:
            self.assertEqual(status, expect, f"{method} {path}: {body[:400]}")
        elif status >= 400:
            self.fail(f"{method} {path} -> {status}: {body[:400]}")
        return body if raw else (json.loads(body) if body else None)

    def test_full_make_to_order_lifecycle(self):
        # Customer order at an agreed price below the catalog price.
        so = self.call("POST", "/api/sales/sales-orders/", {
            "customer": self.customer.id, "items": [{"item": self.widget.id, "quantity": 100, "unit_price": "8.50"}],
        })
        self.call("POST", f"/api/sales/sales-orders/{so['id']}/mark_ready_for_production/")
        wip = self.call("GET", "/api/production/wip/")
        self.assertEqual(len(wip), 1)
        prod = wip[0]
        self.assertEqual((prod["customer_order"], prod["operations_total"]), (f"SO-{so['id']}", 2))

        # Shop floor: schedule, run both operations, report output in two runs.
        self.call("POST", f"/api/production/production-orders/{prod['id']}/schedule/")
        ops = self.call("GET", f"/api/production/production-orders/{prod['id']}/operations/")
        self.call("POST", f"/api/production/operations/{ops[1]['id']}/report/", {"good": 10}, expect=400)
        self.call("POST", f"/api/production/operations/{ops[0]['id']}/report/", {"good": 100, "minutes": 50})
        self.call("POST", f"/api/production/operations/{ops[1]['id']}/report/", {"good": 100, "minutes": 20})
        self.call("POST", f"/api/production/production-orders/{prod['id']}/report_output/", {"quantity": 60})
        self.call("POST", f"/api/production/production-orders/{prod['id']}/report_output/", {"quantity": 40})
        self.assertEqual(Stock.objects.get(item=self.resin).quantity, 0)
        self.assertFalse(Stock.objects.filter(item=self.widget).exists(), "nothing sellable before QA")

        # QA: first output passes, second partly rework.
        checks = sorted(self.call("GET", "/api/quality/quality-checks/?inspection_type=production"), key=lambda c: c["id"])
        self.call("POST", f"/api/quality/quality-checks/{checks[0]['id']}/decide/", {"accepted_quantity": 60})
        self.call("POST", f"/api/quality/quality-checks/{checks[1]['id']}/decide/",
                  {"accepted_quantity": 30, "rework_quantity": 10})
        rework = self.call("POST", f"/api/quality/quality-checks/{checks[1]['id']}/send_to_rework/", expect=201)
        self.call("POST", f"/api/production/production-orders/{rework['id']}/complete/")
        rqc = [c for c in self.call("GET", "/api/quality/quality-checks/") if c["production_order"] == rework["id"]][0]
        self.call("POST", f"/api/quality/quality-checks/{rqc['id']}/decide/", {"accepted_quantity": 10})
        fg = {r["item"]: r for r in self.call("GET", "/api/fulfillment/fg-inventory/")}["Widget"]
        self.assertEqual((fg["on_hand"], fg["available"]), (100, 100))

        # Fulfilment in two partial dispatches, each invoiced at the agreed price.
        self.call("POST", f"/api/fulfillment/orders/{so['id']}/allocate/", expect=201)
        line_id = self.call("GET", f"/api/fulfillment/orders/{so['id']}/")["lines"][0]["order_item_id"]
        invoices = []
        for qty, mode, carrier in ((70, "road", "BlueDart"), (30, "air", "DHL")):
            d = self.call("POST", "/api/fulfillment/dispatches/", {
                "sales_order": so["id"], "lines": [{"order_item_id": line_id, "quantity": qty}]}, expect=201)
            for step, data in (("pick", None), ("pack", {"packages": 3}), ("stage", None),
                               ("prepare", {"shipment_mode": mode, "carrier": carrier, "shipment_reference": f"REF-{qty}"}),
                               ("verify", None), ("confirm", None)):
                d = self.call("POST", f"/api/fulfillment/dispatches/{d['id']}/{step}/", data or {})
            self.assertEqual(d["status"], "dispatched")
            invoices.append(d["invoice"])
            for doc in ("delivery_note", "dispatch_note"):
                html = self.call("GET", f"/api/documents/{doc}/{d['id']}/", raw=True)
                self.assertIn(d["dispatch_number"], html)
        totals = [Decimal(str(self.call("GET", f"/api/sales/invoices/{i}/")["total_amount"])) for i in invoices]
        self.assertEqual(totals, [Decimal("595.00"), Decimal("255.00")])
        self.assertEqual(self.call("GET", f"/api/sales/sales-orders/{so['id']}/")["status"], "delivered")

        # Payment closes AR.
        self.call("POST", f"/api/sales/invoices/{invoices[0]}/record_payment/", {"amount": "595.00"})

        # GL: manufacturing, rework scrap none, 2 COGS, 2 invoices, 1 payment.
        modules = sorted(JournalEntry.objects.filter(company=self.company, status="posted")
                         .values_list("source_module", flat=True))
        self.assertEqual(modules.count("fulfillment.dispatch"), 2)
        self.assertEqual(modules.count("sales.invoice"), 2)
        self.assertIn("manufacturing", modules)
        self.assertIn("sales.payment", modules)

        # Traceability from the invoice reaches every stage.
        trace = self.call("GET", f"/api/insights/traceability/?type=invoice&value=INV-{invoices[1]}")
        stages = {s["key"]: s["count"] for s in trace["results"][0]["stages"]}
        for key in ("production_order", "material_issue", "wip", "qa", "fg", "allocation", "picking",
                    "dispatch", "invoice", "ar", "payment", "gl"):
            self.assertGreater(stages[key], 0, key)

        # Documents and dashboards render.
        for doc, pk in (("production_order", prod["id"]), ("traveler", prod["id"]), ("pick_list", prod["id"]),
                        ("material_requisition", prod["id"]), ("qa_inspection", checks[0]["id"]),
                        ("qa_certificate", checks[0]["id"]), ("invoice", invoices[0])):
            self.assertIn(f"SO-{so['id']}", self.call("GET", f"/api/documents/{doc}/{pk}/", raw=True), doc)
        dash = self.call("GET", "/api/insights/executive/")
        self.assertEqual(dash["orders"]["order_fulfilment_percent"], 100.0)
        self.assertEqual(dash["finance"]["ar_outstanding"], 255.0)
        cost = self.call("GET", f"/api/production/production-orders/{prod['id']}/costing/")
        self.assertGreater(cost["actual"]["resource_cost"], 0)
