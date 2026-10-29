from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Company, User
from . import engine, gst, reports, seeds
from .models import AccountingMode, Ledger, TaxCode, Voucher, VoucherLine

D = Decimal
TODAY = date(2026, 6, 15)


class Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme Forge")
        self.other = Company.objects.create(name="Other Co")
        self.admin = User.objects.create_user("admin.admin@acme", password="x", role="admin", company=self.company)
        seeds.seed_company(self.company, state_code="27", gstin="27AAAAA0000A1Z5", today=TODAY)
        seeds.seed_company(self.other, state_code="29", today=TODAY)
        self.customer = Ledger.objects.create(
            company=self.company, code="C001", name="Customer A", group=self.company.inacc_groups.get(name="Sundry Debtors"),
            control_type="receivable")
        self.vendor = Ledger.objects.create(
            company=self.company, code="S001", name="Supplier A", group=self.company.inacc_groups.get(name="Sundry Creditors"),
            control_type="payable", state_code="27")

    def L(self, key):
        return engine.mapped_ledger(self.company, key)

    def sale(self, pos="27", **kw):
        doc = gst.calculate_document([dict(quantity=1, unit_price=10000, rate=18)], seller_state="27", place_of_supply=pos)
        return engine.post_tax_invoice(self.company, self.admin, kind="sales", voucher_date=TODAY,
                                       party_ledger=self.customer, place_of_supply=pos, doc=doc, seller_state="27", **kw)


class GstTests(TestCase):
    def test_intra_state_split(self):
        r = gst.calculate_line(quantity=1, unit_price=10000, rate=18, seller_state="27", place_of_supply="27")
        self.assertEqual([(c["component"], c["amount"]) for c in r["components"]],
                         [("CGST", D("900.00")), ("SGST", D("900.00"))])
        self.assertEqual(r["line_total"], D("11800.00"))

    def test_inter_state_igst_only(self):
        r = gst.calculate_line(quantity=1, unit_price=10000, rate=18, seller_state="27", place_of_supply="29")
        self.assertEqual([c["component"] for c in r["components"]], ["IGST"])
        self.assertEqual(r["tax_total"], D("1800.00"))

    def test_missing_place_of_supply_rejected(self):
        with self.assertRaises(ValidationError):
            gst.calculate_line(quantity=1, unit_price=100, rate=18, seller_state="27", place_of_supply="")

    def test_tax_inclusive(self):
        r = gst.calculate_line(quantity=1, unit_price=11800, rate=18, seller_state="27", place_of_supply="29",
                               price_includes_tax=True)
        self.assertEqual(r["taxable_value"], D("10000.00"))

    def test_exempt_has_no_tax(self):
        r = gst.calculate_line(quantity=1, unit_price=100, rate=18, seller_state="27", place_of_supply="27", treatment="exempt")
        self.assertEqual(r["tax_total"], D("0.00"))


class PostingTests(Base):
    def test_sales_invoice_entries_and_receipt(self):
        v = self.sale()
        self.assertEqual(v.status, "posted")
        bal = {l.ledger.name: (l.debit, l.credit) for l in v.lines.select_related("ledger")}
        self.assertEqual(bal["Customer A"], (D("11800.00"), D("0.00")))
        self.assertEqual(bal["Sales"], (D("0.00"), D("10000.00")))
        self.assertEqual(bal["Output CGST"][1], D("900.00"))
        self.assertEqual(bal["Output SGST"][1], D("900.00"))
        self.assertEqual(v.tax_lines.count(), 2)
        r = engine.create_voucher(self.company, self.admin, voucher_type="receipt", voucher_date=TODAY, post=True,
                                  lines=[{"ledger": self.L("bank"), "debit": 11800}, {"ledger": self.customer, "credit": 11800}])
        self.assertEqual(engine.ledger_balance(self.customer), D("0.00"))
        self.assertTrue(r.voucher_number.startswith("RV/"))
        # revenue recognised exactly once
        self.assertEqual(-engine.ledger_balance(self.L("sales")), D("10000.00"))

    def test_purchase_with_ineligible_itc_capitalised(self):
        from .models import TaxCode
        tc = TaxCode.objects.get(company=self.company, code="GST18")
        tc.itc_eligible = False
        doc = gst.calculate_document([dict(quantity=1, unit_price=10000, rate=18)], seller_state="27", place_of_supply="27")
        v = engine.post_tax_invoice(self.company, self.admin, kind="purchase", voucher_date=TODAY, party_ledger=self.vendor,
                                    place_of_supply="27", doc=doc, seller_state="27", tax_code=tc, supplier_invoice_no="INV1")
        self.assertEqual(engine.ledger_balance(self.L("purchases")), D("11800.00"))
        self.assertEqual(engine.ledger_balance(self.L("input_cgst")), D("0.00"))
        with self.assertRaises(ValidationError):  # duplicate supplier invoice
            engine.create_voucher(
                self.company, self.admin, voucher_type="purchase", voucher_date=TODAY, party_ledger=self.vendor,
                supplier_invoice_no="INV1",
                lines=[{"ledger": self.L("purchases"), "debit": 1}, {"ledger": self.vendor, "credit": 1}])

    def test_unbalanced_rejected(self):
        with self.assertRaises(ValidationError):
            engine.create_voucher(self.company, self.admin, voucher_type="journal", voucher_date=TODAY,
                                  lines=[{"ledger": self.L("cash"), "debit": 100}, {"ledger": self.L("sales"), "credit": 90}])

    def test_cross_company_ledger_rejected(self):
        foreign = Ledger.objects.get(company=self.other, code="1001")
        with self.assertRaises(ValidationError):
            engine.create_voucher(self.company, self.admin, voucher_type="journal", voucher_date=TODAY,
                                  lines=[{"ledger": foreign, "debit": 5}, {"ledger": self.L("sales"), "credit": 5}])

    def test_source_idempotency(self):
        src = {"module": "sales", "document_type": "invoice", "document_id": 7, "event": "sales"}
        a = self.sale(source=src)
        b = self.sale(source=src)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Voucher.objects.filter(company=self.company).count(), 1)

    def test_posted_voucher_immutable(self):
        v = self.sale()
        v.narration = "tamper"
        with self.assertRaises(ValidationError):
            v.save()
        with self.assertRaises(ValidationError):
            v.delete()
        with self.assertRaises(ValidationError):
            v.lines.first().delete()

    def test_reversal_nets_to_zero_and_links(self):
        v = self.sale()
        rev = engine.reverse_voucher(v.pk, self.admin, self.company, "wrong customer")
        v.refresh_from_db()
        self.assertEqual(v.status, "reversed")
        self.assertEqual(rev.reversal_of_id, v.pk)
        self.assertEqual(engine.ledger_balance(self.customer), D("0.00"))
        with self.assertRaises(ValidationError):
            engine.reverse_voucher(v.pk, self.admin, self.company, "again")
        # source slot is freed so a corrected replacement can be posted
        self.assertTrue(reports.trial_balance(self.company, date(2026, 4, 1), date(2027, 3, 31))["is_balanced"])

    def test_locked_period_blocks_posting(self):
        from .models import AccountingPeriod
        AccountingPeriod.objects.filter(company=self.company, start_date__lte=TODAY, end_date__gte=TODAY).update(status="locked")
        with self.assertRaises(ValidationError):
            self.sale()

    def test_no_financial_year_rejected(self):
        with self.assertRaises(ValidationError):
            engine.create_voucher(self.company, self.admin, voucher_type="journal", voucher_date=date(2020, 1, 1),
                                  lines=[{"ledger": self.L("cash"), "debit": 1}, {"ledger": self.L("sales"), "credit": 1}])

    def test_maker_checker(self):
        v = engine.create_voucher(self.company, self.admin, voucher_type="journal", voucher_date=TODAY,
                                  lines=[{"ledger": self.L("cash"), "debit": 5}, {"ledger": self.L("sales"), "credit": 5}])
        engine.submit_voucher(v.pk, self.admin, self.company)
        with self.assertRaises(ValidationError):
            engine.approve_voucher(v.pk, self.admin, self.company)

    def test_reports_reconcile(self):
        self.sale()
        tb = reports.trial_balance(self.company, date(2026, 4, 1), date(2027, 3, 31))
        self.assertTrue(tb["is_balanced"])
        pl = reports.profit_and_loss(self.company, date(2026, 4, 1), date(2027, 3, 31))
        self.assertEqual(pl["net_profit"], D("10000.00"))
        bs = reports.balance_sheet(self.company, date(2027, 3, 31))
        self.assertTrue(bs["is_balanced"])
        self.assertEqual(reports.general_ledger(self.company, self.customer, date(2026, 4, 1), date(2027, 3, 31))["closing"],
                         D("11800.00"))


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.fresh = Company.objects.create(name="Legacy Co")
        self.legacy_admin = User.objects.create_user("a.admin@legacy", password="x", role="admin", company=self.fresh)
        self.client = APIClient()

    def test_default_mode_is_standard_and_indian_api_blocked(self):
        self.client.force_authenticate(self.legacy_admin)
        self.assertEqual(self.client.get("/api/indian-accounting/mode/").json()["mode"], "standard")
        self.assertEqual(self.client.get("/api/indian-accounting/reports/trial-balance/").status_code, 403)

    def test_choose_indian_seeds_and_locks_choice(self):
        self.client.force_authenticate(self.legacy_admin)
        self.assertEqual(self.client.post("/api/indian-accounting/mode/", {"mode": "indian"}).status_code, 400)
        r = self.client.post("/api/indian-accounting/mode/", {"mode": "indian", "state_code": "27"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(Ledger.objects.filter(company=self.fresh).exists())
        self.assertEqual(self.client.post("/api/indian-accounting/mode/", {"mode": "standard"}).status_code, 409)

    def test_standard_choice_creates_no_indian_data(self):
        self.client.force_authenticate(self.legacy_admin)
        self.assertEqual(self.client.post("/api/indian-accounting/mode/", {"mode": "standard"}).status_code, 201)
        self.assertFalse(Ledger.objects.filter(company=self.fresh).exists())
        self.assertEqual(AccountingMode.objects.get(company=self.fresh).mode, "standard")

    def test_company_isolation_and_invoice_api(self):
        AccountingMode.objects.create(company=self.company, mode="indian")
        self.client.force_authenticate(self.admin)
        foreign = Ledger.objects.get(company=self.other, code="1001")
        r = self.client.get(f"/api/indian-accounting/reports/ledger/?ledger={foreign.pk}")
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/indian-accounting/invoices/sales/", {
            "party_ledger": self.customer.pk, "voucher_date": "2026-06-15", "place_of_supply": "29",
            "lines": [{"quantity": "1", "unit_price": "10000"}], "tax_code": "GST18", "idempotency_key": "k1"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["total"], "11800.00")
        r2 = self.client.post("/api/indian-accounting/invoices/sales/", {
            "party_ledger": self.customer.pk, "voucher_date": "2026-06-15", "place_of_supply": "29",
            "lines": [{"quantity": "1", "unit_price": "10000"}], "tax_code": "GST18", "idempotency_key": "k1"}, format="json")
        self.assertEqual(r.json()["id"], r2.json()["id"])
        self.assertEqual(Voucher.objects.filter(company=self.company).count(), 1)


class OperationalRoutingTests(Base):
    """Real ERP documents flowing through the legacy queue_auto_post entry point."""

    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")
        from sales.models import Customer
        self.cust = Customer.objects.create(company=self.company, name="Bharat Steel")

    def invoice(self, tax="1800.00"):
        from sales.models import Invoice
        return Invoice.objects.create(company=self.company, customer=self.cust, invoice_date=TODAY,
                                      subtotal=D("10000"), tax_amount=D(tax), total_amount=D("10000") + D(tax))

    def route(self, event, obj):
        from accounting.auto_posting import queue_auto_post
        from accounting.models import JournalEntry
        with self.captureOnCommitCallbacks(execute=True):
            queue_auto_post(event, self.company, obj.id, self.admin)
        self.assertEqual(JournalEntry.objects.filter(company=self.company).count(), 0)  # legacy books untouched

    def test_invoice_needs_state_then_posts_on_retry(self):
        from .models import PostingEvent
        inv = self.invoice()
        self.route("sales_invoice", inv)
        ev = PostingEvent.objects.get(company=self.company, event="sales_invoice")
        self.assertEqual(ev.status, "failed")
        self.assertIn("state code", ev.message)
        Ledger.objects.filter(company=self.company, customer=self.cust).update(state_code="29")  # inter-state
        from . import hooks
        ev = hooks.execute("sales_invoice", self.company, inv.id, self.admin)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual(engine.ledger_balance(self.L("output_igst")), D("-1800.00"))
        self.assertEqual(engine.ledger_balance(self.L("sales")), D("-10000.00"))
        hooks.execute("sales_invoice", self.company, inv.id, self.admin)  # replay is a no-op
        self.assertEqual(Voucher.objects.filter(company=self.company).count(), 1)

    def test_unknown_rate_is_not_guessed(self):
        from .models import PostingEvent
        inv = self.invoice(tax="777.00")
        self.route("sales_invoice", inv)
        Ledger.objects.filter(company=self.company, customer=self.cust).update(state_code="27")
        from . import hooks
        ev = hooks.execute("sales_invoice", self.company, inv.id, self.admin)
        self.assertEqual(ev.status, "failed")
        self.assertIn("not a configured GST rate", ev.message)

    def test_payment_and_bill_and_unsupported_event(self):
        from sales.models import CustomerPayment
        from procurement.models import Bill, Vendor
        from .models import PostingEvent
        inv = self.invoice()
        self.route("sales_invoice", inv)
        Ledger.objects.filter(company=self.company, customer=self.cust).update(state_code="27")
        from . import hooks
        hooks.execute("sales_invoice", self.company, inv.id, self.admin)
        pay = CustomerPayment.objects.create(company=self.company, customer=self.cust, invoice=inv, amount=D("11800"),
                                             payment_date=TODAY)
        self.route("customer_payment", pay)
        party = Ledger.objects.get(company=self.company, customer=self.cust)
        self.assertEqual(engine.ledger_balance(party), D("0.00"))
        bill = Bill.objects.create(company=self.company, vendor=Vendor.objects.create(company=self.company, name="Ore Co"),
                                   bill_number="B-1", bill_date=TODAY, total_amount=D("500"))
        self.route("vendor_bill", bill)
        self.assertEqual(engine.ledger_balance(self.L("purchases")), D("500.00"))
        self.route("some_future_event", bill)  # unsupported -> skipped, never booked in legacy books
        self.assertEqual(PostingEvent.objects.get(company=self.company, event="some_future_event").status, "skipped")

    def test_standard_company_still_uses_legacy(self):
        from accounting.auto_posting import queue_auto_post
        from .models import PostingEvent
        queue_auto_post("sales_invoice", self.other, 1, self.admin)
        self.assertFalse(PostingEvent.objects.filter(company=self.other).exists())


class InventoryBase(Base):
    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")
        from inventory.models import Item, Warehouse
        from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor
        from production.models import Recipe, RecipeIngredient
        from sales.models import Customer
        self.wh = Warehouse.objects.create(company=self.company, name="Plant", location="HQ")
        self.sugar = Item.objects.create(company=self.company, name="Sugar", category="raw_material", purchase_cost=D("2.00"))
        self.soda = Item.objects.create(company=self.company, name="Soda", category="finished_good", selling_price=D("5.00"))
        self.recipe = Recipe.objects.create(product=self.soda, batch_size=10)
        RecipeIngredient.objects.create(recipe=self.recipe, item=self.sugar, quantity=5)  # std cost 1.00 / bottle
        self.vendor_m = Vendor.objects.create(company=self.company, name="Sweet Supplies")
        self.po = PurchaseOrder.objects.create(vendor=self.vendor_m, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=self.po, item=self.sugar, quantity=100, unit_price=D("2.00"))
        self.cust = Customer.objects.create(company=self.company, name="Corner Store")

    def run_event(self, event, source_id, payload=None):
        from . import hooks
        return hooks.execute(event, self.company, source_id, self.admin, payload or {})

    def bal(self, key):
        return engine.ledger_balance(self.L(key))

class InventoryCycleTests(InventoryBase):
    def test_purchase_cycle_clears_grni(self):
        from procurement.models import Bill, GoodsReceipt
        r = GoodsReceipt.objects.create(purchase_order=self.po, warehouse=self.wh)
        ev = self.run_event("goods_receipt", r.id)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("raw_material"), self.bal("grni")), (D("200.00"), D("-200.00")))
        bill = Bill.objects.create(company=self.company, vendor=self.vendor_m, purchase_order=self.po,
                                   bill_number="SS-1", bill_date=TODAY, total_amount=D("200.00"))
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual(self.bal("grni"), D("0.00"))          # fully cleared
        self.assertEqual(self.bal("purchases"), D("0.00"))     # no tax, nothing beyond what was received
        vendor_ledger = Ledger.objects.get(company=self.company, vendor=self.vendor_m)
        self.assertEqual(engine.ledger_balance(vendor_ledger), D("-200.00"))

    def test_production_scrap_and_cogs_policies(self):
        from production.models import ProductionOrder
        o = ProductionOrder.objects.create(recipe=self.recipe, quantity=20, warehouse=self.wh)
        ev = self.run_event("production_completed", o.id, {"completed_qty": 18, "scrap_qty": 2})
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("finished_goods"), self.bal("scrap"), self.bal("raw_material")),
                         (D("18.00"), D("2.00"), D("-20.00")))
        # default policy = recognise COGS on invoice, so a shipment books nothing
        self.assertEqual(self.run_event("sales_shipment", 1, {"lines": []}).status, "skipped")
        # invoice policy: COGS booked with the invoice
        from sales.models import Invoice, InvoiceLine
        Ledger.objects.filter(company=self.company, customer=None).count()
        inv = Invoice.objects.create(company=self.company, customer=self.cust, invoice_date=TODAY,
                                     subtotal=D("50"), tax_amount=D("0"), total_amount=D("50"))
        InvoiceLine.objects.create(invoice=inv, item=self.soda, quantity=10, unit_price=D("5"), amount=D("50"))
        self.run_event("sales_invoice", inv.id)  # fails: no place of supply yet
        Ledger.objects.filter(company=self.company, customer=self.cust).update(state_code="27")
        ev = self.run_event("sales_invoice", inv.id)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("cogs"), self.bal("finished_goods")), (D("10.00"), D("8.00")))

    def test_dispatch_policy_uses_dispatch_not_invoice(self):
        from .models import IndianCompanyProfile
        IndianCompanyProfile.objects.filter(company=self.company).update(cogs_on="dispatch")
        ev = self.run_event("sales_shipment", 1, {"lines": []})
        self.assertEqual(ev.status, "failed")  # no such order: validated, nothing booked
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(customer=self.cust, status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=5)
        ev = self.run_event("sales_shipment", so.id, {"lines": [{"item_id": self.soda.id, "quantity": 5}]})
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("cogs"), self.bal("finished_goods")), (D("5.00"), D("-5.00")))


class BillGstBase(InventoryBase):
    def make_bill(self, total="236.00"):
        from procurement.models import Bill, GoodsReceipt
        r = GoodsReceipt.objects.create(purchase_order=self.po, warehouse=self.wh)
        self.assertEqual(self.run_event("goods_receipt", r.id).status, "posted")
        return Bill.objects.create(company=self.company, vendor=self.vendor_m, purchase_order=self.po,
                                   bill_number="SS-9", bill_date=TODAY, total_amount=D(total))

    def set_gst(self, bill, state="27", **kw):
        from .models import BillTaxDetail, TaxCode
        return BillTaxDetail.objects.create(
            company=self.company, bill=bill, tax_code=TaxCode.objects.get(company=self.company, code="GST18"),
            taxable_value=D("200"), supplier_state=state, **kw)

class BillGstTests(BillGstBase):
    def test_intra_state_bill_claims_itc_and_clears_grni(self):
        bill = self.make_bill()
        self.set_gst(bill)
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("input_cgst"), self.bal("input_sgst"), self.bal("input_igst")),
                         (D("18.00"), D("18.00"), D("0.00")))
        self.assertEqual((self.bal("grni"), self.bal("purchases")), (D("0.00"), D("0.00")))
        self.assertEqual(engine.ledger_balance(Ledger.objects.get(company=self.company, vendor=self.vendor_m)), D("-236.00"))
        self.assertEqual(ev.voucher.tax_lines.count(), 2)

    def test_inter_state_bill_uses_igst_and_ineligible_itc_is_capitalised(self):
        bill = self.make_bill()
        self.set_gst(bill, state="29", itc_eligible=False)
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual(self.bal("input_igst"), D("0.00"))       # no credit claimed
        self.assertEqual(self.bal("grni"), D("0.00"))             # 200 cleared
        self.assertEqual(self.bal("purchases"), D("36.00"))       # tax capitalised beyond received value

    def test_total_mismatch_is_rejected(self):
        bill = self.make_bill(total="300.00")
        self.set_gst(bill)
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "failed")
        self.assertIn("does not match", ev.message)

    def test_require_gst_holds_bill_until_details_entered(self):
        from .models import IndianCompanyProfile
        IndianCompanyProfile.objects.filter(company=self.company).update(require_bill_gst=True)
        bill = self.make_bill()
        self.assertEqual(self.run_event("vendor_bill", bill.id).status, "failed")
        self.set_gst(bill)
        from . import hooks
        self.assertEqual(hooks.execute("vendor_bill", self.company, bill.id, self.admin).status, "posted")


class BillGstApiTests(BillGstBase):
    def test_api_sets_details_then_conflicts_after_posting(self):
        from .models import TaxCode
        c = APIClient()
        c.force_authenticate(self.admin)
        bill = self.make_bill()
        tc = TaxCode.objects.get(company=self.company, code="GST18")
        body = {"bill": bill.id, "tax_code": tc.id, "taxable_value": "200", "supplier_state": "27"}
        self.assertEqual(c.post("/api/indian-accounting/bill-tax/", body, format="json").status_code, 201)
        self.assertEqual(c.post("/api/indian-accounting/bill-tax/", {**body, "supplier_state": "ab"}, format="json").status_code, 400)
        self.run_event("vendor_bill", bill.id)
        self.assertEqual(c.post("/api/indian-accounting/bill-tax/", body, format="json").status_code, 409)

    def test_settings_policy_locks_after_cogs(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        self.assertEqual(c.patch("/api/indian-accounting/settings/", {"cogs_on": "bogus"}, format="json").status_code, 400)
        self.assertEqual(c.patch("/api/indian-accounting/settings/", {"cogs_on": "dispatch"}, format="json").status_code, 200)
        self.assertEqual(c.get("/api/indian-accounting/settings/").json()["cogs_on"], "dispatch")


class AllocationTests(Base):
    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")

    def receipt(self, amount, on=TODAY):
        return engine.create_voucher(
            self.company, self.admin, voucher_type="receipt", voucher_date=on, post=True, party_ledger=self.customer,
            lines=[{"ledger": self.L("bank"), "debit": amount}, {"ledger": self.customer, "credit": amount}])

    def invoice(self, due=None):
        v = self.sale()
        if due:
            Voucher.objects.filter(pk=v.pk).update(due_date=due)
        return Voucher.objects.get(pk=v.pk)

    def test_partial_then_full_settlement_and_limits(self):
        from . import allocations as al
        inv = self.invoice()
        r1 = self.receipt(5000)
        a1 = al.allocate(self.company, self.admin, payment_id=r1.id, invoice_id=inv.id, amount=5000)
        self.assertEqual(al.outstanding(inv), D("6800.00"))
        with self.assertRaises(ValidationError):  # receipt fully used
            al.allocate(self.company, self.admin, payment_id=r1.id, invoice_id=inv.id, amount=1)
        r2 = self.receipt(10000)
        with self.assertRaises(ValidationError):  # more than outstanding
            al.allocate(self.company, self.admin, payment_id=r2.id, invoice_id=inv.id, amount=7000)
        al.allocate(self.company, self.admin, payment_id=r2.id, invoice_id=inv.id, amount=6800)
        self.assertEqual(al.outstanding(inv), D("0.00"))
        self.assertEqual(al.unallocated(r2), D("3200.00"))           # advance stays visible
        self.assertEqual(al.open_items(self.company, "sales"), [])
        al.release(self.company, self.admin, a1.id, "wrong invoice")
        self.assertEqual(al.outstanding(inv), D("5000.00"))
        with self.assertRaises(ValidationError):
            al.release(self.company, self.admin, a1.id, "again")

    def test_party_and_type_mismatch_rejected(self):
        from . import allocations as al
        inv = self.invoice()
        other = Ledger.objects.create(company=self.company, code="C002", name="Customer B", control_type="receivable",
                                      group=self.customer.group)
        r = engine.create_voucher(self.company, self.admin, voucher_type="receipt", voucher_date=TODAY, post=True,
                                  party_ledger=other, lines=[{"ledger": self.L("bank"), "debit": 100},
                                                            {"ledger": other, "credit": 100}])
        with self.assertRaises(ValidationError):
            al.allocate(self.company, self.admin, payment_id=r.id, invoice_id=inv.id, amount=100)
        pay = engine.create_voucher(self.company, self.admin, voucher_type="payment", voucher_date=TODAY, post=True,
                                    party_ledger=self.customer, lines=[{"ledger": self.customer, "debit": 100},
                                                                      {"ledger": self.L("bank"), "credit": 100}])
        with self.assertRaises(ValidationError):  # a payment cannot settle a sales invoice
            al.allocate(self.company, self.admin, payment_id=pay.id, invoice_id=inv.id, amount=100)

    def test_reversal_releases_allocations_and_hides_invoice(self):
        from . import allocations as al
        inv = self.invoice()
        r = self.receipt(11800)
        a = al.allocate(self.company, self.admin, payment_id=r.id, invoice_id=inv.id, amount=11800)
        engine.reverse_voucher(r.id, self.admin, self.company, "bounced cheque")
        a.refresh_from_db()
        self.assertEqual(a.status, "released")
        self.assertEqual(al.outstanding(inv), D("11800.00"))
        engine.reverse_voucher(inv.id, self.admin, self.company, "cancelled")
        self.assertEqual(al.open_items(self.company, "sales"), [])   # neither original nor its reversal is open

    def test_ageing_buckets_and_advances(self):
        from datetime import timedelta
        from . import allocations as al
        self.invoice(due=TODAY - timedelta(days=45))
        self.receipt(1000)
        rep = al.ageing(self.company, "sales", TODAY)
        self.assertEqual(rep["totals"]["31-60"], D("11800.00"))
        self.assertEqual(rep["totals"]["current"], D("0.00"))
        self.assertEqual(rep["parties"][0]["advance"], D("1000.00"))
        self.assertEqual(rep["grand_total"], D("11800.00"))

    def test_statement_lists_open_items_and_running_balance(self):
        from . import allocations as al
        self.invoice()
        st = al.statement(self.company, self.customer, date(2026, 4, 1), date(2027, 3, 31))
        self.assertEqual(st["closing"], D("11800.00"))
        self.assertEqual(len(st["open_items"]), 1)

    def test_api_allocate_release_and_ageing(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        inv = self.invoice()
        r = self.receipt(2000)
        res = c.post("/api/indian-accounting/allocations/", {"payment": r.id, "invoice": inv.id, "amount": "2000"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        res = c.post("/api/indian-accounting/allocations/", {"payment": r.id, "invoice": inv.id, "amount": "1"}, format="json")
        self.assertEqual(res.status_code, 400)
        aid = c.get("/api/indian-accounting/allocations/").json()[0]["id"]
        self.assertEqual(c.post(f"/api/indian-accounting/allocations/{aid}/release/", {}, format="json").status_code, 400)  # reason needed
        self.assertEqual(c.post(f"/api/indian-accounting/allocations/{aid}/release/", {"reason": "typo"}, format="json").status_code, 200)
        rep = c.get("/api/indian-accounting/reports/receivables-ageing/?to=2026-06-15").json()
        self.assertEqual(D(str(rep["grand_total"])), D("11800.00"))

    def test_linked_erp_payment_settles_its_invoice_only(self):
        from sales.models import Customer, CustomerPayment, Invoice
        from . import allocations as al, hooks
        cust = Customer.objects.create(company=self.company, name="Bharat Steel")
        inv = Invoice.objects.create(company=self.company, customer=cust, invoice_date=TODAY, subtotal=D("10000"),
                                     tax_amount=D("1800"), total_amount=D("11800"))
        hooks.execute("sales_invoice", self.company, inv.id, self.admin)  # creates ledger, fails (no state)
        Ledger.objects.filter(company=self.company, customer=cust).update(state_code="27")
        self.assertEqual(hooks.execute("sales_invoice", self.company, inv.id, self.admin).status, "posted")
        pay = CustomerPayment.objects.create(company=self.company, customer=cust, invoice=inv, amount=D("15000"),
                                             payment_date=TODAY)
        self.assertEqual(hooks.execute("customer_payment", self.company, pay.id, self.admin).status, "posted")
        iv = Voucher.objects.get(company=self.company, source_document_type="invoice", voucher_type="sales")
        rc = Voucher.objects.get(company=self.company, voucher_type="receipt")
        self.assertEqual(al.outstanding(iv), D("0.00"))
        self.assertEqual(al.unallocated(rc), D("3200.00"))   # overpayment is an advance, not forced onto anything


class NoteTests(Base):
    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")

    def test_credit_note_reverses_revenue_and_tax_proportionally(self):
        from . import allocations as al, notes
        inv = self.sale()
        n = notes.post_note(self.company, self.admin, original_id=inv.id, taxable_value="2000",
                            voucher_date=TODAY, reason="damaged goods returned")
        self.assertEqual(n.voucher_type, "credit_note")
        self.assertEqual(n.total_amount, D("2360.00"))
        self.assertEqual(engine.ledger_balance(self.L("sales_returns")), D("2000.00"))
        self.assertEqual(engine.ledger_balance(self.L("output_cgst")), D("-720.00"))   # 900 - 180
        self.assertEqual(engine.ledger_balance(self.customer), D("9440.00"))
        self.assertEqual(al.outstanding(inv), D("9440.00"))
        self.assertEqual(n.tax_lines.count(), 2)
        with self.assertRaises(ValidationError):   # would exceed what is left of the invoice
            notes.post_note(self.company, self.admin, original_id=inv.id, taxable_value="9000",
                            voucher_date=TODAY, reason="too much")
        # settlement can no longer exceed the net balance
        r = engine.create_voucher(self.company, self.admin, voucher_type="receipt", voucher_date=TODAY, post=True,
                                  party_ledger=self.customer, lines=[{"ledger": self.L("bank"), "debit": 11800},
                                                                    {"ledger": self.customer, "credit": 11800}])
        with self.assertRaises(ValidationError):
            al.allocate(self.company, self.admin, payment_id=r.id, invoice_id=inv.id, amount=9441)
        al.allocate(self.company, self.admin, payment_id=r.id, invoice_id=inv.id, amount=9440)
        self.assertEqual(al.open_items(self.company, "sales"), [])
        # reversing the note restores the receivable
        engine.reverse_voucher(n.id, self.admin, self.company, "entered in error")
        self.assertEqual(al.outstanding(inv), D("2360.00"))   # 11800 - 9440 paid, note no longer counts

    def test_debit_note_reverses_input_credit(self):
        from . import notes
        doc = gst.calculate_document([dict(quantity=1, unit_price=1000, rate=18)], seller_state="27", place_of_supply="27")
        bill = engine.post_tax_invoice(self.company, self.admin, kind="purchase", voucher_date=TODAY, party_ledger=self.vendor,
                                       place_of_supply="27", doc=doc, seller_state="27")
        n = notes.post_note(self.company, self.admin, original_id=bill.id, taxable_value="500",
                            voucher_date=TODAY, reason="rejected lot")
        self.assertEqual(n.voucher_type, "debit_note")
        self.assertEqual(engine.ledger_balance(self.L("input_cgst")), D("45.00"))     # 90 - 45
        self.assertEqual(engine.ledger_balance(self.L("purchases")), D("500.00"))
        self.assertEqual(engine.ledger_balance(self.vendor), D("-590.00"))             # 1180 - 590

    def test_note_requires_posted_original_and_reason_and_api(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        inv = self.sale()
        body = {"against": inv.id, "taxable_value": "1000", "voucher_date": "2026-06-15", "reason": "price correction"}
        res = c.post("/api/indian-accounting/notes/", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(c.post("/api/indian-accounting/notes/", {**body, "reason": ""}, format="json").status_code, 400)
        engine.reverse_voucher(inv.id, self.admin, self.company, "wrong party")
        self.assertEqual(c.post("/api/indian-accounting/notes/", body, format="json").status_code, 400)


class ClosingTests(Base):
    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")
        from .models import FinancialYear
        self.fy = FinancialYear.objects.get(company=self.company, name="2026-27")

    def test_close_blocked_by_pending_work_then_succeeds_and_blocks_posting(self):
        from . import closing
        from .models import FinancialYear, PostingEvent
        self.sale()
        draft = engine.create_voucher(self.company, self.admin, voucher_type="journal", voucher_date=TODAY,
                                      lines=[{"ledger": self.L("cash"), "debit": 5}, {"ledger": self.L("sales"), "credit": 5}])
        PostingEvent.objects.create(company=self.company, event="vendor_bill", source_id="1", status="failed", message="x")
        with self.assertRaises(ValidationError) as cm:
            closing.close_year(self.company, self.admin, self.fy.id)
        self.assertEqual(len(cm.exception.messages), 2)
        Voucher.objects.filter(pk=draft.pk).update(status="cancelled")
        PostingEvent.objects.all().delete()
        closing.close_year(self.company, self.admin, self.fy.id)
        self.fy.refresh_from_db()
        self.assertEqual(self.fy.status, "closed")
        self.assertFalse(self.fy.periods.exclude(status="locked").exists())
        self.assertTrue(FinancialYear.objects.filter(company=self.company, name="2027-28").exists())
        with self.assertRaises(ValidationError):
            self.sale()
        with self.assertRaises(ValidationError):   # reason required
            closing.reopen_year(self.company, self.admin, self.fy.id, "")
        closing.reopen_year(self.company, self.admin, self.fy.id, "late purchase invoice")
        with self.assertRaises(ValidationError):   # year open again but periods are still locked
            self.sale()
        p = self.fy.periods.get(start_date__lte=TODAY, end_date__gte=TODAY)
        with self.assertRaises(ValidationError):
            closing.set_period_lock(self.company, self.admin, p.id, False, "")
        closing.set_period_lock(self.company, self.admin, p.id, False, "late invoice")
        self.sale()

    def test_next_year_resets_nominal_ledgers_and_keeps_trial_balance_balanced(self):
        from . import closing
        self.sale()
        closing.close_year(self.company, self.admin, self.fy.id)
        tb = reports.trial_balance(self.company, date(2027, 4, 1), date(2028, 3, 31))
        self.assertTrue(tb["is_balanced"])
        names = {r["name"]: r for r in tb["rows"]}
        self.assertNotIn("Sales", names)                                      # restarts at zero
        self.assertEqual(names["Accumulated profit/loss brought forward"]["opening_credit"], D("10000.00"))
        self.assertEqual(names["Customer A"]["opening_debit"], D("11800.00"))  # balance sheet carries forward
        bs = reports.balance_sheet(self.company, date(2028, 3, 31))
        self.assertTrue(bs["is_balanced"])

    def test_close_api_is_admin_only(self):
        fin = User.objects.create_user("fin.finance@acme", password="x", role="finance", company=self.company)
        c = APIClient()
        c.force_authenticate(fin)
        self.assertEqual(c.post(f"/api/indian-accounting/periods/year/{self.fy.id}/close/", {}, format="json").status_code, 403)
        c.force_authenticate(self.admin)
        self.assertEqual(c.post(f"/api/indian-accounting/periods/year/{self.fy.id}/close/", {}, format="json").status_code, 200)
        self.assertEqual(c.get("/api/indian-accounting/periods/").json()[-1]["status"], "closed")


class OpeningImportTests(TestCase):
    CSV = "code,name,group,side,amount\n1001,Cash,Cash-in-Hand,Dr,50000\n2001,Capital,Capital Account,Cr,50000\n"

    def setUp(self):
        self.company = Company.objects.create(name="Fresh Co")
        self.admin = User.objects.create_user("a.admin@fresh", password="x", role="admin", company=self.company)
        seeds.seed_company(self.company, state_code="27", today=TODAY)
        AccountingMode.objects.create(company=self.company, mode="indian")
        self.c = APIClient()
        self.c.force_authenticate(self.admin)

    def upload(self, text, name="ob.csv"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.c.post("/api/indian-accounting/import/opening-balances/",
                           {"file": SimpleUploadedFile(name, text.encode())}, format="multipart")

    def test_preview_commit_and_no_second_import(self):
        r = self.upload(self.CSV)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["errors"], [])
        self.assertEqual(Ledger.objects.get(company=self.company, code="1001").opening_balance, D("0.00"))  # preview writes nothing
        bid = r.json()["id"]
        self.assertEqual(self.c.post(f"/api/indian-accounting/import/opening-balances/{bid}/commit/", {}, format="json").status_code, 200)
        cash = Ledger.objects.get(company=self.company, code="1001")
        self.assertEqual((cash.opening_balance, cash.opening_side), (D("50000.00"), "Dr"))
        self.assertTrue(reports.trial_balance(self.company, date(2026, 4, 1), date(2027, 3, 31))["is_balanced"])
        self.assertEqual(self.upload(self.CSV).status_code, 400)                      # same file again
        self.assertEqual(self.upload(self.CSV + "1002,Bank,Bank Accounts,Dr,1\n").status_code, 400)  # opening already loaded

    def test_unbalanced_and_bad_rows_are_reported_and_cannot_commit(self):
        bad = ("code,name,group,side,amount\n1001,Cash,Cash-in-Hand,Dr,100\n"
               "3001,Mystery,No Such Group,Dr,5\n1001,Cash,Cash-in-Hand,Dr,1\n1002,Bank,Bank Accounts,X,5\n")
        r = self.upload(bad)
        errs = [e["message"] for e in r.json()["errors"]]
        self.assertTrue(any("Unknown group" in m for m in errs))
        self.assertTrue(any("Duplicate code" in m for m in errs))
        self.assertTrue(any("Dr or Cr" in m for m in errs))
        bid = r.json()["id"]
        self.assertEqual(self.c.post(f"/api/indian-accounting/import/opening-balances/{bid}/commit/", {}, format="json").status_code, 400)
        r = self.upload("code,name,group,side,amount\n1001,Cash,Cash-in-Hand,Dr,100\n")
        self.assertTrue(any("do not balance" in e["message"] for e in r.json()["errors"]))

    def test_rejected_after_vouchers_exist_and_for_non_admin(self):
        engine.create_voucher(self.company, self.admin, voucher_type="journal", voucher_date=TODAY, post=True,
                              lines=[{"ledger": engine.mapped_ledger(self.company, "cash"), "debit": 5},
                                     {"ledger": engine.mapped_ledger(self.company, "sales"), "credit": 5}])
        self.assertEqual(self.upload(self.CSV).status_code, 400)
        fin = User.objects.create_user("f.finance@fresh", password="x", role="finance", company=self.company)
        self.c.force_authenticate(fin)
        self.assertEqual(self.upload(self.CSV).status_code, 403)


class GeoTests(TestCase):
    def test_gstin_and_address_derivation(self):
        from . import geo
        self.assertEqual(geo.derive("27AAAAA0000A1Z5")[:3], ("27", "27AAAAA0000A1Z5", "gstin"))
        self.assertEqual(geo.derive("", "Plot 5, MIDC, Pune, Maharashtra 411019"), ("27", "", "address"))
        self.assertEqual(geo.derive("12 Ring Rd, New Delhi")[0], "07")              # not mistaken for two places
        self.assertEqual(geo.derive("Odisha")[0], "21")
        self.assertEqual(geo.derive("Mumbai, Maharashtra / Bangalore, Karnataka"), ("", "", ""))  # ambiguous
        self.assertEqual(geo.derive("no state here"), ("", "", ""))
        self.assertEqual(geo.derive("55ZZZZZ0000Z1Z5"), ("", "", ""))               # unknown prefix is not trusted


class DerivedGstTests(BillGstBase):
    def test_customer_state_comes_from_address_and_invoice_posts_without_manual_step(self):
        from sales.models import Customer, Invoice
        cust = Customer.objects.create(company=self.company, name="Gujarat Steels", address="GIDC, Surat, Gujarat 395003")
        inv = Invoice.objects.create(company=self.company, customer=cust, invoice_date=TODAY, subtotal=D("1000"),
                                     tax_amount=D("180"), total_amount=D("1180"))
        ev = self.run_event("sales_invoice", inv.id)
        self.assertEqual(ev.status, "posted", ev.message)
        led = Ledger.objects.get(company=self.company, customer=cust)
        self.assertEqual((led.state_code, led.state_source), ("24", "address"))
        self.assertEqual(self.bal("output_igst"), D("-180.00"))                     # 24 != company state 27

    def test_manual_state_is_never_overwritten(self):
        from sales.models import Customer
        from . import hooks
        cust = Customer.objects.create(company=self.company, name="X", address="Pune, Maharashtra")
        led = hooks._party_ledger(self.company, "customer", cust)
        Ledger.objects.filter(pk=led.pk).update(state_code="29", state_source="manual")
        self.assertEqual(hooks._party_ledger(self.company, "customer", cust).state_code, "29")

    def test_bill_gst_is_derived_from_received_goods_and_total(self):
        self.vendor_m.tax_id = "27AAAAA0000A1Z5"
        self.vendor_m.save()
        bill = self.make_bill(total="236.00")           # goods 200 + 36 tax = 18%
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("input_cgst"), self.bal("input_sgst"), self.bal("grni")),
                         (D("18.00"), D("18.00"), D("0.00")))
        from .models import BillTaxDetail
        self.assertTrue(BillTaxDetail.objects.get(bill=bill).derived)

    def test_unmatched_rate_or_unknown_supplier_state_is_not_guessed(self):
        self.vendor_m.tax_id = "27AAAAA0000A1Z5"
        self.vendor_m.save()
        bill = self.make_bill(total="230.00")           # 15% is not a configured rate
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "failed")
        self.assertIn("not a configured GST rate", ev.message)

    def test_missing_supplier_state_fails_with_instruction(self):
        bill = self.make_bill(total="236.00")           # vendor has no GSTIN / address
        ev = self.run_event("vendor_bill", bill.id)
        self.assertEqual(ev.status, "failed")
        self.assertIn("state code", ev.message)



class VendorPaymentTests(BillGstBase):
    def test_vendor_payment_posts_via_signal_and_settles_its_bill(self):
        from procurement.models import VendorPayment
        from . import allocations as al
        self.vendor_m.tax_id = "27AAAAA0000A1Z5"
        self.vendor_m.save()
        bill = self.make_bill(total="236.00")
        self.assertEqual(self.run_event("vendor_bill", bill.id).status, "posted")
        with self.captureOnCommitCallbacks(execute=True):
            VendorPayment.objects.create(company=self.company, vendor=self.vendor_m, bill=bill, amount=D("100"),
                                         payment_date=TODAY, method="bank_transfer")
        v = Voucher.objects.get(company=self.company, voucher_type="payment")
        bill_v = Voucher.objects.get(company=self.company, voucher_type="purchase")
        self.assertEqual(al.outstanding(bill_v), D("136.00"))
        self.assertEqual(engine.ledger_balance(Ledger.objects.get(company=self.company, vendor=self.vendor_m)), D("-136.00"))
        self.assertEqual(al.unallocated(v), D("0.00"))

    def test_legacy_company_vendor_payment_is_ignored(self):
        from procurement.models import Vendor, VendorPayment
        from accounts.models import Company
        from .models import PostingEvent
        other = Company.objects.create(name="Plain Co")
        vend = Vendor.objects.create(company=other, name="V")
        with self.captureOnCommitCallbacks(execute=True):
            VendorPayment.objects.create(company=other, vendor=vend, amount=D("5"), payment_date=TODAY)
        self.assertFalse(PostingEvent.objects.filter(company=other).exists())


class ProductionCostingTests(InventoryBase):
    def setUp(self):
        super().setUp()
        from production.models import ProductionOrder
        self.order = ProductionOrder.objects.create(recipe=self.recipe, quantity=20, warehouse=self.wh)

    def test_partial_completions_post_only_the_difference_through_wip(self):
        ev = self.run_event("production_completed", self.order.id, {"completed_qty": 10, "scrap_qty": 0})
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual((self.bal("finished_goods"), self.bal("wip"), self.bal("raw_material")),
                         (D("10.00"), D("0.00"), D("-10.00")))
        ev = self.run_event("production_completed", self.order.id, {"completed_qty": 10, "scrap_qty": 0})   # double-click
        self.assertEqual(self.bal("finished_goods"), D("10.00"))
        ev = self.run_event("production_completed", self.order.id, {"completed_qty": 18, "scrap_qty": 2})
        self.assertEqual((self.bal("finished_goods"), self.bal("scrap"), self.bal("raw_material"), self.bal("wip")),
                         (D("18.00"), D("2.00"), D("-20.00"), D("0.00")))
        self.assertEqual(Voucher.objects.filter(company=self.company, source_document_type="order").count(), 2)

    def test_labour_and_overhead_are_absorbed_and_unapproved_units_stay_in_wip(self):
        from unittest import mock
        fake = {"actual": {"raw_material_cost": 0, "materials": [], "labour_cost": 30.0, "machine_cost": 10.0,
                           "operation_cost": 0.0, "overhead_cost": 10.0, "extra_cost": 0.0}}
        self.order.produced_quantity = 20
        self.order.save()
        with mock.patch("production.costing.order_cost", return_value=fake):
            ev = self.run_event("production_completed", self.order.id, {"completed_qty": 10, "scrap_qty": 0})
        self.assertEqual(ev.status, "posted", ev.message)
        # material 10 (standard, the 10 approved units) + labour 30 + overhead 20 = 60 over 20 units produced -> 3.00/unit
        self.assertEqual(self.bal("labour_absorbed"), D("-30.00"))
        self.assertEqual(self.bal("overhead_absorbed"), D("-20.00"))
        self.assertEqual(self.bal("finished_goods"), D("30.00"))      # 10 approved units
        self.assertEqual(self.bal("wip"), D("30.00"))                 # the other 10 units' cost is still work in progress

    def test_weighted_average_issues_and_cogs(self):
        from procurement.models import GoodsReceipt
        from .models import IndianCompanyProfile
        from . import valuation
        IndianCompanyProfile.objects.filter(company=self.company).update(costing_method="weighted_average")
        r = GoodsReceipt.objects.create(purchase_order=self.po, warehouse=self.wh)
        self.run_event("goods_receipt", r.id)                                   # 100 sugar @ 2.00
        self.assertEqual(valuation.average_cost(self.company, self.sugar), D("2.0000"))
        # second purchase at a higher price changes the average
        valuation.record(self.company, self.sugar, 100, 400, "grn", None, TODAY)   # 100 @ 4.00
        self.assertEqual(valuation.average_cost(self.company, self.sugar), D("3.0000"))
        self.assertEqual(valuation.issue_value(self.company, self.sugar, 50, 9), D("150.00"))
        # shortfall beyond stock is valued at the fallback unit cost
        self.assertEqual(valuation.issue_value(self.company, self.sugar, 250, 9), D("1050.00"))

    def test_costing_method_locks_after_stock_is_issued(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        self.assertEqual(c.patch("/api/indian-accounting/settings/", {"costing_method": "weighted_average"}, format="json").status_code, 200)
        self.run_event("production_completed", self.order.id, {"completed_qty": 10, "scrap_qty": 0})
        from .models import InventoryMovement
        InventoryMovement.objects.create(company=self.company, item=self.soda, qty=1, value=1, kind="production_in",
                                         movement_date=TODAY)
        self.assertEqual(c.patch("/api/indian-accounting/settings/", {"costing_method": "standard"}, format="json").status_code, 409)

    def test_reversing_a_cogs_voucher_restores_stock_valuation(self):
        from . import valuation
        valuation.record(self.company, self.soda, 10, 100, "opening", None, TODAY)
        from .models import IndianCompanyProfile
        IndianCompanyProfile.objects.filter(company=self.company).update(costing_method="weighted_average", cogs_on="dispatch")
        from sales.models import SalesOrder, SalesOrderItem
        so = SalesOrder.objects.create(customer=self.cust, status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=4)
        ev = self.run_event("sales_shipment", so.id, {"lines": [{"item_id": self.soda.id, "quantity": 4}]})
        self.assertEqual(ev.status, "posted", ev.message)
        self.assertEqual(valuation.on_hand(self.company, self.soda), (D("6"), D("60.00")))
        engine.reverse_voucher(ev.voucher_id, self.admin, self.company, "wrong shipment")
        self.assertEqual(valuation.on_hand(self.company, self.soda), (D("10"), D("100.00")))


class DrillDownTests(Base):
    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")

    def test_voucher_detail_has_lines_tax_audit_and_links(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        inv = self.sale(source={"module": "sales", "document_type": "invoice", "document_id": 9, "event": "sales"})
        r = c.get(f"/api/indian-accounting/vouchers/{inv.id}/detail/").json()
        self.assertEqual(r["number"], inv.voucher_number)
        self.assertEqual(len(r["lines"]), 4)
        self.assertEqual(len(r["tax_lines"]), 2)
        self.assertEqual(r["source"]["page"], "/sales")
        self.assertTrue(any(a["action"] == "post" for a in r["audit"]))
        rev = engine.reverse_voucher(inv.id, self.admin, self.company, "oops")
        r = c.get(f"/api/indian-accounting/vouchers/{inv.id}/detail/").json()
        self.assertEqual(r["reversed_by"]["id"], rev.id)
        r = c.get(f"/api/indian-accounting/vouchers/{rev.id}/detail/").json()
        self.assertEqual(r["reversal_of"]["id"], inv.id)

    def test_detail_is_company_scoped(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        other = Company.objects.create(name="Spy Co")
        seeds.seed_company(other, state_code="29", today=TODAY)
        v = engine.create_voucher(other, None, voucher_type="journal", voucher_date=TODAY, post=True,
                                  lines=[{"ledger": engine.mapped_ledger(other, "cash"), "debit": 1},
                                         {"ledger": engine.mapped_ledger(other, "sales"), "credit": 1}])
        self.assertEqual(c.get(f"/api/indian-accounting/vouchers/{v.id}/detail/").status_code, 404)


class ImportKindsTests(Base):
    def setUp(self):
        super().setUp()
        AccountingMode.objects.create(company=self.company, mode="indian")
        self.c = APIClient()
        self.c.force_authenticate(self.admin)

    def up(self, kind, text, name="f.csv", raw=None):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.c.post(f"/api/indian-accounting/import/{kind}/",
                           {"file": SimpleUploadedFile(name, raw or text.encode())}, format="multipart")

    def commit(self, kind, res):
        return self.c.post(f"/api/indian-accounting/import/{kind}/{res.json()['id']}/commit/", {}, format="json")

    def test_chart_of_accounts_import(self):
        csv = ("type,name,parent,code,nature,category,control_type\n"
               "group,Plant & Machinery,Fixed Assets,,,,\nledger,Lathe,Plant & Machinery,1501,,,\n"
               "group,Brand New Top,,,asset,current,\n")
        r = self.up("chart-of-accounts", csv)
        self.assertEqual(r.json()["errors"], [], r.content)
        self.assertEqual(self.commit("chart-of-accounts", r).status_code, 200)
        lathe = Ledger.objects.get(company=self.company, code="1501")
        self.assertEqual((lathe.group.name, lathe.group.nature), ("Plant & Machinery", "asset"))   # nature inherited
        bad = self.up("chart-of-accounts", "type,name,parent,code,nature,category,control_type\nledger,Dup,Fixed Assets,1501,,,\n"
                                           "group,Orphan,Nowhere,,,,\n")
        msgs = " | ".join(e["message"] for e in bad.json()["errors"])
        self.assertIn("already exists", msgs)
        self.assertIn("not found", msgs)

    def test_tax_code_import_rejects_overlap_and_accepts_new_rate(self):
        hdr = "code,name,rate,treatment,hsn,itc_eligible,effective_from,effective_to\n"
        bad = self.up("tax-codes", hdr + "GST18,Again,18,taxable,,yes,2020-01-01,\n")
        self.assertIn("end-date", bad.json()["errors"][0]["message"])
        ok = self.up("tax-codes", hdr + "GST3,Gold 3%,3,taxable,7108,yes,01/04/2026,\n")
        self.assertEqual(ok.json()["errors"], [])
        self.assertEqual(self.commit("tax-codes", ok).status_code, 200)
        self.assertEqual(TaxCode.objects.get(company=self.company, code="GST3").hsn_sac, "7108")

    def test_open_invoices_must_reconcile_and_then_become_open_items(self):
        from . import allocations as al
        hdr = "type,party_code,number,date,due_date,amount\n"
        mismatch = self.up("open-invoices", hdr + "sales,C001,OLD-1,2026-03-10,2026-04-10,500\n")
        self.assertTrue(any("opening balance" in e["message"] for e in mismatch.json()["errors"]))
        # give the customer an opening balance, then the listed documents must add up to it
        ob = self.up("opening-balances", "code,name,group,side,amount\nC001,Customer A,Sundry Debtors,Dr,800\n"
                                         "2001,Capital,Capital Account,Cr,800\n")
        self.assertEqual(self.commit("opening-balances", ob).status_code, 200)
        r = self.up("open-invoices", hdr + "sales,C001,OLD-1,2026-03-10,2026-04-10,500\nsales,C001,OLD-2,2026-03-15,2026-04-15,300\n")
        self.assertEqual(r.json()["errors"], [], r.content)
        self.assertEqual(self.commit("open-invoices", r).status_code, 200)
        items = al.open_items(self.company, "sales", date(2026, 6, 1))
        self.assertEqual(sorted(i["number"] for i in items), ["OLD-1", "OLD-2"])
        self.assertEqual(reports.trial_balance(self.company, date(2026, 4, 1), date(2027, 3, 31))["totals"]["cd"],
                         D("800.00"))                               # opening docs add nothing to the ledger
        old = Voucher.objects.get(company=self.company, voucher_number="OLD-1")
        with self.assertRaises(ValidationError):
            engine.reverse_voucher(old.id, self.admin, self.company, "x")
        # a receipt can settle an opening invoice
        rcpt = engine.create_voucher(self.company, self.admin, voucher_type="receipt", voucher_date=TODAY, post=True,
                                     party_ledger=self.customer, lines=[{"ledger": self.L("bank"), "debit": 500},
                                                                       {"ledger": self.customer, "credit": 500}])
        al.allocate(self.company, self.admin, payment_id=rcpt.id, invoice_id=old.id, amount=500)
        self.assertEqual(al.outstanding(old), D("0.00"))
        self.assertEqual(len(reports.day_book(self.company, date(2026, 1, 1), date(2027, 3, 31))), 1)   # only the receipt

    def test_opening_stock_import_reconciles_and_feeds_valuation(self):
        from inventory.models import Item
        from . import valuation
        sugar = Item.objects.create(company=self.company, name="Sugar", category="raw_material", purchase_cost=D("2"))
        hdr = "item,class,qty,value\n"
        bad = self.up("opening-stock", hdr + "Sugar,raw_material,100,200\nGhost,raw_material,1,1\n")
        self.assertTrue(any("not found" in e["message"] for e in bad.json()["errors"]))
        mismatch = self.up("opening-stock", hdr + "Sugar,raw_material,100,200\n")
        self.assertTrue(any("ledger opens at" in e["message"] for e in mismatch.json()["errors"]))
        ob = self.up("opening-balances", "code,name,group,side,amount\n1201,Raw Material Stock,Stock-in-Hand,Dr,200\n"
                                         "2001,Capital,Capital Account,Cr,200\n")
        self.commit("opening-balances", ob)
        ok = self.up("opening-stock", hdr + "Sugar,raw_material,100,200\n", name="stock2.csv")
        self.assertEqual(ok.json()["errors"], [], ok.content)
        self.assertEqual(self.commit("opening-stock", ok).status_code, 200)
        self.assertEqual(valuation.average_cost(self.company, sugar), D("2.0000"))
        self.assertEqual(self.up("opening-stock", hdr + "Sugar,raw_material,100,200\n", name="s3.csv").status_code, 400)

    def test_excel_upload_is_supported(self):
        import io, openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["code", "name", "rate", "effective_from"])
        ws.append(["GST3", "Gold 3%", 3, date(2026, 4, 1)])
        buf = io.BytesIO()
        wb.save(buf)
        r = self.up("tax-codes", "", name="rates.xlsx", raw=buf.getvalue())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["errors"], [], r.content)
        self.assertEqual(r.json()["rows"][0]["effective_from"], "2026-04-01")
        self.assertEqual(self.up("tax-codes", "", name="bad.xlsx", raw=b"not an excel file").status_code, 400)

    def test_template_download_and_admin_only(self):
        r = self.c.get("/api/indian-accounting/import/open-invoices/?template=1")
        self.assertIn("party_code", r.content.decode())
        fin = User.objects.create_user("f.finance@acme", password="x", role="finance", company=self.company)
        self.c.force_authenticate(fin)
        self.assertEqual(self.up("tax-codes", "code,name,rate,effective_from\nX,X,1,2026-01-01\n").status_code, 403)
