from decimal import Decimal
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Company, User
from inventory.models import Item, Warehouse, Stock, BOM, BOMLine
from production.models import ProductionPlan
from sales.models import Customer, SalesOrder
from .models import (
    PurchaseOrder, PurchaseOrderItem, Vendor, VendorPriceList,
    PurchaseRequisition, PurchaseRequisitionItem,
)
from .serializers import PurchaseOrderItemSerializer, VendorPriceListSerializer


class ProcurementRawMaterialValidationTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(name="Packaging Supplier")
        self.raw_item = Item.objects.create(name="Aluminum Can", category="raw_material", unit="pcs")
        self.finished_item = Item.objects.create(name="Sparkling Water", category="finished_good", unit="bottle")
        self.purchase_order = PurchaseOrder.objects.create(vendor=self.vendor)

    def test_vendor_price_list_only_accepts_raw_materials(self):
        serializer = VendorPriceListSerializer(
            data={
                "vendor": self.vendor.id,
                "item": self.finished_item.id,
                "unit_price": "2.50",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Only raw materials", str(serializer.errors["item"][0]))

    def test_purchase_order_line_only_accepts_raw_materials(self):
        serializer = PurchaseOrderItemSerializer(
            data={
                "purchase_order": self.purchase_order.id,
                "item": self.finished_item.id,
                "quantity": 10,
                "unit_price": "2.50",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Only raw materials", str(serializer.errors["item"][0]))

    def test_purchase_order_line_accepts_raw_materials(self):
        serializer = PurchaseOrderItemSerializer(
            data={
                "purchase_order": self.purchase_order.id,
                "item": self.raw_item.id,
                "quantity": 10,
                "unit_price": "2.50",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class PurchaseRequisitionMRPTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name="Apex Brewery Co")
        self.store_user = User.objects.create_user(
            username="store_mgr", password="password123", role="store", company=self.company
        )
        self.client.force_authenticate(user=self.store_user)

        self.warehouse = Warehouse.objects.create(
            name="Raw Warehouse", location="Bldg A", company=self.company, is_quarantine=False
        )
        self.vendor = Vendor.objects.create(
            name="Hop & Grain Supplier", company=self.company
        )

        self.rm_item = Item.objects.create(
            name="Malt Extract", category="raw_material", unit="kg", company=self.company, purchase_cost=Decimal("10.00")
        )
        self.fg_item = Item.objects.create(
            name="Pale Ale", category="finished_good", unit="bottle", company=self.company
        )

        # Active BOM: 1 Pale Ale requires 2 kg Malt Extract
        self.bom = BOM.objects.create(finished_good=self.fg_item, version="1.0", is_active=True)
        BOMLine.objects.create(bom=self.bom, raw_material=self.rm_item, quantity=2.0, unit="kg")

        # Initial stock: 50 kg available in warehouse
        Stock.objects.create(item=self.rm_item, warehouse=self.warehouse, quantity=50.0)

        # Customer, SalesOrder, ProductionPlan: 100 bottles planned -> requires 200 kg Malt Extract (shortage = 150 kg)
        self.customer = Customer.objects.create(name="Distributor One", company=self.company)
        self.sales_order = SalesOrder.objects.create(customer=self.customer, total_amount=2500.0)
        self.plan = ProductionPlan.objects.create(
            plan_number="PLAN-2026-001",
            company=self.company,
            sales_order=self.sales_order,
            customer=self.customer,
            item=self.fg_item,
            order_quantity=100.0,
            planned_quantity=100.0,
        )

    def test_create_requisition_from_mrp_shortage(self):
        """TEST 1 — CREATE REQUISITION FROM MRP SHORTAGE: creates PurchaseRequisition and items with correct fields"""
        url = "/api/procurement/requisitions/create_from_mrp/"
        data = {
            "production_plan_id": self.plan.id,
            "warehouse_id": self.warehouse.id,
            "items": [
                {
                    "item_id": self.rm_item.id,
                    "required_quantity": 200.0,
                    "available_quantity": 50.0,
                    "shortage_quantity": 150.0,
                    "uom": "kg",
                }
            ],
        }
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data["created"])

        req = PurchaseRequisition.objects.get(production_plan=self.plan)
        self.assertEqual(req.status, "approved")
        self.assertEqual(req.production_plan, self.plan)
        self.assertEqual(req.company, self.company)
        self.assertEqual(req.warehouse, self.warehouse)
        self.assertEqual(req.sales_order, self.sales_order)
        self.assertEqual(req.items.count(), 1)

        req_item = req.items.first()
        self.assertEqual(req_item.item, self.rm_item)
        self.assertEqual(req_item.required_quantity, 200.0)
        self.assertEqual(req_item.available_quantity, 50.0)
        self.assertEqual(req_item.shortage_quantity, 150.0)
        self.assertEqual(req_item.uom, "kg")

    def test_create_from_plan_without_items(self):
        """TEST 2 — CREATE FROM PLAN WITHOUT ITEMS: endpoint calculates MRP dynamically and extracts shortages"""
        url = "/api/procurement/requisitions/create_from_mrp/"
        data = {
            "production_plan_id": self.plan.id,
            "warehouse_id": self.warehouse.id,
        }
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data["created"])

        req = PurchaseRequisition.objects.get(production_plan=self.plan)
        self.assertEqual(req.items.count(), 1)
        req_item = req.items.first()
        self.assertEqual(req_item.item, self.rm_item)
        # 100 bottles * 2 kg = 200 kg required - 50 kg available = 150 kg shortage
        self.assertEqual(req_item.required_quantity, 200.0)
        self.assertEqual(req_item.available_quantity, 50.0)
        self.assertEqual(req_item.shortage_quantity, 150.0)

    def test_no_shortage_does_not_create_empty_requisition(self):
        """TEST 3 — NO SHORTAGE: sufficient inventory prevents creating an empty requisition"""
        Stock.objects.filter(item=self.rm_item, warehouse=self.warehouse).update(quantity=500.0)
        url = "/api/procurement/requisitions/create_from_mrp/"
        data = {
            "production_plan_id": self.plan.id,
            "warehouse_id": self.warehouse.id,
        }
        res = self.client.post(url, data, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data["created"])
        self.assertIn("No material shortages detected", res.data["message"])
        self.assertEqual(PurchaseRequisition.objects.filter(production_plan=self.plan).count(), 0)

    def test_duplicate_procurement_protection(self):
        """TEST 4 — DUPLICATE PROTECTION: repeated calls return existing requisition without duplicate records"""
        url = "/api/procurement/requisitions/create_from_mrp/"
        data = {"production_plan_id": self.plan.id}

        res1 = self.client.post(url, data, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res1.data["created"])
        self.assertEqual(PurchaseRequisition.objects.filter(production_plan=self.plan).count(), 1)

        res2 = self.client.post(url, data, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertFalse(res2.data["created"])
        self.assertIn("already exists", res2.data["message"])
        self.assertEqual(PurchaseRequisition.objects.filter(production_plan=self.plan).count(), 1)

    def test_convert_requisition_to_po(self):
        """TEST 5 — CONVERT REQUISITION TO PO: creates PO, sets quantities from shortage, marks requisition converted"""
        req = PurchaseRequisition.objects.create(
            company=self.company,
            warehouse=self.warehouse,
            production_plan=self.plan,
            sales_order=self.sales_order,
            status="approved",
            created_by=self.store_user,
        )
        PurchaseRequisitionItem.objects.create(
            requisition=req,
            item=self.rm_item,
            required_quantity=200.0,
            available_quantity=50.0,
            shortage_quantity=150.0,
            uom="kg",
            estimated_unit_price=Decimal("12.00"),
        )
        VendorPriceList.objects.create(
            vendor=self.vendor,
            item=self.rm_item,
            unit_price=Decimal("12.00"),
            min_order_qty=50.0,
        )

        url = f"/api/procurement/requisitions/{req.id}/convert_to_po/"
        res = self.client.post(url, {"vendor_id": self.vendor.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        req.refresh_from_db()
        self.assertEqual(req.status, "converted")

        po = PurchaseOrder.objects.get(requisition=req)
        self.assertEqual(po.vendor, self.vendor)
        self.assertEqual(po.status, "pending")
        self.assertEqual(po.items.count(), 1)

        po_item = po.items.first()
        self.assertEqual(po_item.item, self.rm_item)
        self.assertEqual(po_item.quantity, 150.0)
        self.assertEqual(po_item.unit_price, Decimal("12.00"))
        self.assertEqual(po.total_amount, Decimal("1800.00"))

    def test_production_plan_and_sales_order_traceability(self):
        """TEST 6 — PRODUCTION PLAN TRACEABILITY: PO preserves links to requisition, production plan, and sales order"""
        req = PurchaseRequisition.objects.create(
            company=self.company,
            warehouse=self.warehouse,
            production_plan=self.plan,
            sales_order=self.sales_order,
            status="approved",
            created_by=self.store_user,
        )
        PurchaseRequisitionItem.objects.create(
            requisition=req,
            item=self.rm_item,
            required_quantity=200.0,
            available_quantity=50.0,
            shortage_quantity=150.0,
            uom="kg",
            estimated_unit_price=Decimal("10.00"),
        )

        res = self.client.post(f"/api/procurement/requisitions/{req.id}/convert_to_po/", format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        po = PurchaseOrder.objects.get(requisition=req)
        self.assertEqual(po.requisition.production_plan, self.plan)
        self.assertEqual(po.requisition.sales_order, self.sales_order)
        self.assertIn(self.plan.plan_number, po.notes)

    def test_prevent_double_conversion(self):
        """TEST 7 — PREVENT DOUBLE CONVERSION: attempting to convert a converted requisition is rejected"""
        req = PurchaseRequisition.objects.create(
            company=self.company,
            warehouse=self.warehouse,
            production_plan=self.plan,
            sales_order=self.sales_order,
            status="approved",
            created_by=self.store_user,
        )
        PurchaseRequisitionItem.objects.create(
            requisition=req,
            item=self.rm_item,
            required_quantity=200.0,
            available_quantity=50.0,
            shortage_quantity=150.0,
            uom="kg",
        )

        url = f"/api/procurement/requisitions/{req.id}/convert_to_po/"
        res1 = self.client.post(url, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(PurchaseOrder.objects.filter(requisition=req).count(), 1)

        res2 = self.client.post(url, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been converted", res2.data["error"])
        self.assertEqual(PurchaseOrder.objects.filter(requisition=req).count(), 1)

    def test_company_isolation(self):
        """TEST 8 — COMPANY ISOLATION: prevents cross-company requisition creation and conversion"""
        company_b = Company.objects.create(name="Second Company LLC")
        user_b = User.objects.create_user(
            username="store_other", password="password123", role="store", company=company_b
        )
        self.client.force_authenticate(user=user_b)

        # Cross-company create from MRP
        res_create = self.client.post(
            "/api/procurement/requisitions/create_from_mrp/",
            {"production_plan_id": self.plan.id},
            format="json",
        )
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)

        # Cross-company convert requisition
        req_a = PurchaseRequisition.objects.create(
            company=self.company,
            warehouse=self.warehouse,
            production_plan=self.plan,
            status="approved",
        )
        res_convert = self.client.post(
            f"/api/procurement/requisitions/{req_a.id}/convert_to_po/",
            format="json",
        )
        self.assertEqual(res_convert.status_code, status.HTTP_404_NOT_FOUND)
