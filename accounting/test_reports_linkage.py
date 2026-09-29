"""Financial reports reflect buying and selling items in the ERP.

Drives a full cycle through the operational endpoints (purchase -> receive ->
bill -> produce -> ship -> invoice -> payment) with GL auto-posting on, then
checks the Profit & Loss and Balance Sheet read the result from the ledger.
"""
from decimal import Decimal

from rest_framework import status

from procurement.models import PurchaseOrder, PurchaseOrderItem
from production.models import ProductionOrder
from sales.models import SalesOrder, SalesOrderItem

from .models import AccountingSettings
from .reports import get_balance_sheet_report, get_profit_and_loss_report
from .test_auto_posting import AutoPostingTestBase


class ReportsReflectErpActivityTests(AutoPostingTestBase):
    def call(self, method, url, data=None, expected=(200, 201)):
        with self.captureOnCommitCallbacks(execute=True):
            res = getattr(self.client, method)(url, data or {}, format="json")
        self.assertIn(res.status_code, expected, res.data)
        return res

    def run_cycle(self):
        # Buy 100 kg sugar at 2.00 -> receive (the vendor bill is created on receipt)
        po = PurchaseOrder.objects.create(vendor=self.vendor, status="ordered")
        PurchaseOrderItem.objects.create(purchase_order=po, item=self.sugar, quantity=100, unit_price=Decimal("2.00"))
        self.call("post", "/api/procurement/goods-receipts/", {"purchase_order": po.id, "warehouse": self.warehouse.id})

        # Make 20 bottles (10 kg sugar = 20.00), sell them at 5.00, collect 40.00
        order = ProductionOrder.objects.create(recipe=self.recipe, quantity=20, warehouse=self.warehouse)
        self.call("post", f"/api/production/production-orders/{order.id}/complete/")
        so = SalesOrder.objects.create(customer=self.customer, status="confirmed")
        SalesOrderItem.objects.create(sales_order=so, item=self.soda, quantity=20)
        self.call("post", f"/api/sales/sales-orders/{so.id}/fulfill_order/")
        invoice = self.call("post", f"/api/sales/sales-orders/{so.id}/generate_invoice/").data
        self.call("post", f"/api/sales/invoices/{invoice['id']}/record_payment/", {"amount": "40.00"})

    def test_profit_and_loss_shows_sales_revenue_and_cost_of_goods_sold(self):
        self.run_cycle()
        pnl = get_profit_and_loss_report(self.company)
        sections = pnl["sections"]
        self.assertEqual(Decimal(str(sections["revenue"]["total"])), Decimal("100.00"))
        self.assertEqual(Decimal(str(sections["cogs"]["total"])), Decimal("20.00"))
        self.assertEqual(Decimal(str(sections["gross_profit"]["amount"])), Decimal("80.00"))

    def test_balance_sheet_balances_and_carries_stock_receivables_and_payables(self):
        self.run_cycle()
        bs = get_balance_sheet_report(self.company)
        self.assertTrue(bs["is_balanced"], bs.get("discrepancy"))
        balances = {
            row["code"]: Decimal(str(row["amount"]))
            for section in bs["sections"].values() if isinstance(section, dict)
            for row in section.get("accounts", [])
        }
        self.assertEqual(balances.get("1210"), Decimal("180.00"))  # 200 bought - 20 used
        self.assertEqual(balances.get("1100"), Decimal("60.00"))   # 100 invoiced - 40 paid
        self.assertEqual(balances.get("2010"), Decimal("200.00"))  # unpaid vendor bill

    def test_nothing_reaches_the_reports_when_auto_posting_is_off(self):
        AccountingSettings.objects.filter(company=self.company).update(auto_post_enabled=False)
        self.run_cycle()
        pnl = get_profit_and_loss_report(self.company)
        self.assertEqual(Decimal(str(pnl["sections"]["revenue"]["total"])), Decimal("0.00"))
