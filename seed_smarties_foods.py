"""
seed_smarties_foods.py   v2
============================
Creates the complete Smarties Foods Manufacturing Pvt. Ltd. demo company
in the PRODUCTION database with all MTO workflow data:

  Company       : Smarties Foods Manufacturing Pvt. Ltd.
  Admin login   : admin@smartiesfoods.com  / SmartiesDemo@2026
  Extra users   : production@smartiesfoods.com / SmartiesDemo@2026
                  quality@smartiesfoods.com    / SmartiesDemo@2026
                  store@smartiesfoods.com      / SmartiesDemo@2026

Run:
    python seed_smarties_foods.py

Target a different server:
    ERP_API_BASE=http://127.0.0.1:8000/api python seed_smarties_foods.py

After this script the DB will contain:
  * Warehouses      : Raw Material WH, Production WH, Finished Goods WH
  * Items           : 12 raw materials + Cookie (SFG) + Smarties Cookies (FG)
  * BOMs            : BOM-1 (Cookie), BOM-2 (Smarties Cookies)
  * Recipes         : Cookie, Smarties Cookies (with routing steps)
  * Vendors         : 5 suppliers
  * Customer        : ABC Retail Foods Pvt. Ltd.
  * Sales Order     : CO-2026-001  (confirmed, 1,000 units, 30 Nov 2026)
  * Production Plan : PP-2026-001  (approved, planned)
  * MRP shortages   : Dates short by 20 kg (intentional)
  * Purchase Req    : PR-2026-001  for Dates shortfall
  * Purchase Order  : PO-2026-001  for 20 kg Dates
  * Goods Receipt   : GRN received into Raw Material WH
  * Production Order: PRD-xxxxx (running / WIP)
  * Operations      : Mixing ✓, Baking ✓, Cooling 70%, Packing 0%, Inspection 0%
  * Invoice         : INV-2026-001 at ₹700/unit
  * Resources       : Mixer-01, Oven-01, Cooling-Rack-01, Packing-Machine-01
  * Operators       : Rahul, Priya, Ankit, Neha
"""

import os, sys, django, time
from datetime import date, datetime, timedelta
from decimal import Decimal

# ── Django setup ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "freshfizz_erp.settings")
django.setup()

# ── imports after django.setup() ───────────────────────────────────────────────
from django.db import transaction
from django.utils import timezone

from accounts.models import Company, User
from inventory.models import Item, Warehouse, Stock, BOM, BOMLine, StockMovement
from procurement.models import (
    Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem,
    GoodsReceipt, PurchaseRequisition, PurchaseRequisitionItem,
)
from production.models import (
    ProductionLine, Recipe, RecipeIngredient, RoutingStep,
    ProductionOrder, ProductionOperation, ProductionPlan,
    ProductionMaterialRequirement, ManufacturingSettings, Resource,
)
from sales.models import (
    Customer, SalesOrder, SalesOrderItem, Invoice, InvoiceLine, CustomerPayment,
)
from quality.models import QualityCheck, IncomingQualityCheck
from fulfillment.models import FGAllocation, Dispatch, DispatchLine

PASSWORD = "SmartiesDemo@2026"
COMPANY_NAME = "Smarties Foods Manufacturing Pvt. Ltd."
COMPANY_SLUG = "smartiesfoods"

# ── Helper ─────────────────────────────────────────────────────────────────────
def log(msg):
    print(f"  ✓ {msg}")

def get_or_create_company():
    co, created = Company.objects.get_or_create(
        slug=COMPANY_SLUG,
        defaults={"name": COMPANY_NAME}
    )
    if not created:
        co.name = COMPANY_NAME
        co.save(update_fields=["name"])
    return co

def get_or_create_user(username, email, first_name, last_name, company, role):
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "role": role,
            "is_staff": True,
        }
    )
    # Role gates the whole UI (sidebar + dashboards), so make sure it is set
    # even on users created by an earlier version of this seeder.
    if user.role != role:
        user.role = role
    user.set_password(PASSWORD)
    user.save()
    return user, created

def get_or_create_warehouse(company, name, location, wh_type):
    wh, _ = Warehouse.objects.get_or_create(
        company=company, name=name,
        defaults={"location": location, "warehouse_type": wh_type}
    )
    return wh

def get_or_create_item(company, name, category, unit, sku, cost, selling_price=0):
    item, _ = Item.objects.get_or_create(
        company=company, name=name,
        defaults={
            "category": category,
            "unit": unit,
            "sku": sku,
            "purchase_cost": Decimal(str(cost)),
            "selling_price": Decimal(str(selling_price)),
        }
    )
    return item

def set_stock(item, warehouse, qty):
    stock, _ = Stock.objects.get_or_create(item=item, warehouse=warehouse, defaults={"quantity": qty})
    if stock.quantity != qty:
        stock.quantity = qty
        stock.save(update_fields=["quantity"])
    return stock

def get_or_create_vendor(company, name, email, phone, address, rating):
    vendor, _ = Vendor.objects.get_or_create(
        company=company, name=name,
        defaults={"email": email, "phone": phone, "address": address, "rating": rating}
    )
    return vendor

def get_or_create_vendor_price(vendor, item, price, moq, lead):
    vp, _ = VendorPriceList.objects.get_or_create(
        vendor=vendor, item=item,
        defaults={"unit_price": Decimal(str(price)), "min_order_qty": moq, "lead_time_days": lead, "currency": "INR"}
    )
    return vp

def get_or_create_resource(company, name, rtype, category, line, cost_hr, run_rate=0):
    res, _ = Resource.objects.get_or_create(
        company=company, name=name,
        defaults={
            "resource_type": rtype,
            "category": category,
            "line": line,
            "cost_per_hour": Decimal(str(cost_hr)),
            "run_rate_per_hour": run_rate,
            "status": "available",
        }
    )
    return res

def build_bom(finished_good, lines_data):
    """lines_data = [(item, qty_per_unit, unit_str), ...]"""
    bom, _ = BOM.objects.get_or_create(
        finished_good=finished_good,
        defaults={"is_active": True, "version": "1.0", "status": "active"}
    )
    for raw_mat, qty, unit in lines_data:
        BOMLine.objects.get_or_create(
            bom=bom, raw_material=raw_mat,
            defaults={"quantity": qty, "unit": unit}
        )
    return bom

def build_recipe(item, batch_size, line, routing_steps_data):
    """routing_steps_data = [(seq, name, machine_res, manpower_res, setup_min, run_min_per_unit), ...]"""
    recipe, _ = Recipe.objects.get_or_create(
        product=item,
        defaults={"batch_size": batch_size, "default_line": line}
    )
    for seq, name, machine_res, manpower_res, setup_min, run_min in routing_steps_data:
        RoutingStep.objects.get_or_create(
            recipe=recipe, sequence=seq,
            defaults={
                "name": name,
                "machine": machine_res,
                "manpower": manpower_res,
                "setup_minutes": setup_min,
                "run_minutes_per_unit": run_min,
            }
        )
    return recipe

# ══════════════════════════════════════════════════════════════════════════════
# MAIN SEEDER
# ══════════════════════════════════════════════════════════════════════════════
@transaction.atomic
def seed():
    print(f"\n{'═'*65}")
    print(f"  Seeding: {COMPANY_NAME}")
    print(f"{'═'*65}")

    # ── 1. Company ─────────────────────────────────────────────────────────────
    co = get_or_create_company()
    log(f"Company: {co.name}  (slug={co.slug})")

    # ── 2. Manufacturing Settings ──────────────────────────────────────────────
    ms = ManufacturingSettings.for_company(co)
    ms.business_model = "mto"
    ms.incoming_qc_required = True
    ms.enforce_operation_sequence = True
    ms.auto_invoice_on_dispatch = True
    ms.allow_partial_production_and_dispatch = True
    ms.save()
    log("Manufacturing settings: MTO, incoming QC enabled")

    # ── 3. Users ───────────────────────────────────────────────────────────────
    admin_user, _ = get_or_create_user(
        f"admin@{COMPANY_SLUG}", "admin@smartiesfoods.com",
        "Sunita", "Sharma", co, "admin"
    )
    prod_user, _ = get_or_create_user(
        f"production@{COMPANY_SLUG}", "production@smartiesfoods.com",
        "Vijay", "Patel", co, "production"
    )
    qual_user, _ = get_or_create_user(
        f"quality@{COMPANY_SLUG}", "quality@smartiesfoods.com",
        "Meera", "Iyer", co, "quality"
    )
    store_user, _ = get_or_create_user(
        f"store@{COMPANY_SLUG}", "store@smartiesfoods.com",
        "Arjun", "Nair", co, "store"
    )
    log(f"Users: admin / production / quality / store  (password: {PASSWORD})")

    # ── 4. Warehouses ──────────────────────────────────────────────────────────
    wh_rm   = get_or_create_warehouse(co, "Raw Material Warehouse",    "Smarties Foods - Plant 1, Mumbai", "raw_material")
    wh_prod = get_or_create_warehouse(co, "Production Warehouse",       "Smarties Foods - Plant 1, Mumbai", "production")
    wh_fg   = get_or_create_warehouse(co, "Finished Goods Warehouse",   "Smarties Foods - Dispatch Area",  "finished_goods")
    log(f"Warehouses: {wh_rm.name}, {wh_prod.name}, {wh_fg.name}")

    # ── 5. Raw Material Items ──────────────────────────────────────────────────
    rm_white_flour   = get_or_create_item(co, "White Flour",       "raw_material", "kg",   "RM-001",  32)
    rm_brown_flour   = get_or_create_item(co, "Brown Flour",       "raw_material", "kg",   "RM-002",  38)
    rm_sugar         = get_or_create_item(co, "Sugar",             "raw_material", "kg",   "RM-003",  45)
    rm_coconut       = get_or_create_item(co, "Coconut",           "raw_material", "kg",   "RM-004",  80)
    rm_dates         = get_or_create_item(co, "Dates",             "raw_material", "kg",   "RM-005", 120)
    rm_almonds       = get_or_create_item(co, "Almonds",           "raw_material", "kg",   "RM-006", 500)
    rm_flavoring     = get_or_create_item(co, "Flavoring",         "raw_material", "kg",   "RM-007", 400)
    rm_art_colors    = get_or_create_item(co, "Artificial Colors", "raw_material", "kg",   "RM-008", 350)
    rm_smarties      = get_or_create_item(co, "Smarties",          "raw_material", "kg",   "RM-009", 200)
    rm_label         = get_or_create_item(co, "Label",             "raw_material", "unit", "RM-010",   2)
    rm_bag           = get_or_create_item(co, "Bag",               "raw_material", "unit", "RM-011",   5)
    rm_carton        = get_or_create_item(co, "Carton",            "raw_material", "unit", "RM-012",  30)

    # ── 6. Semi-Finished Item (Cookie) ─────────────────────────────────────────
    sfg_cookie = get_or_create_item(co, "Cookie", "intermediate", "unit", "SFG-001", 85)

    # ── 7. Finished Good (Smarties Cookies) ────────────────────────────────────
    fg_smarties = get_or_create_item(co, "Smarties Cookies", "finished_good", "unit", "FG-001", 500, selling_price=700)

    log(f"Items: 12 raw materials + Cookie (SFG) + Smarties Cookies (FG)")

    # ── 8. Opening Stock (enough for demo, intentionally short on Dates) ───────
    #  BOM-1 needs per 100 Cookie:
    #    White Flour 50 kg, Brown Flour 20 kg, Sugar 15 kg, Coconut 5 kg,
    #    Dates 5 kg, Almonds 5 kg, Flavoring 1 kg, Artificial Colors 0.5 kg
    #
    #  For 1,000 Smarties Cookies → need 1,000 Cookie → need 10× BOM-1:
    #    White Flour 500 kg, Brown Flour 200 kg, Sugar 150 kg, Coconut 50 kg,
    #    Dates 50 kg (only 30 available → SHORT 20 kg), Almonds 50 kg,
    #    Flavoring 10 kg, Artificial Colors 5 kg
    #
    #  BOM-2 per 100 Smarties Cookies:
    #    Cookie 100, Smarties 10 kg, Label 100, Bag 100, Carton 10

    set_stock(rm_white_flour,  wh_rm, 500)   # exact need → Available
    set_stock(rm_brown_flour,  wh_rm, 200)   # exact need → Available
    set_stock(rm_sugar,        wh_rm, 150)   # exact need → Available
    set_stock(rm_coconut,      wh_rm,  50)   # exact need → Available
    set_stock(rm_dates,        wh_rm,  30)   # need 50 → SHORT 20 kg
    set_stock(rm_almonds,      wh_rm,  50)   # exact need → Available
    set_stock(rm_flavoring,    wh_rm,  10)   # exact need → Available
    set_stock(rm_art_colors,   wh_rm,   5)   # exact need → Available
    set_stock(rm_smarties,     wh_rm, 100)   # exact need → Available
    set_stock(rm_label,        wh_rm, 1000)  # Available
    set_stock(rm_bag,          wh_rm, 1000)  # Available
    set_stock(rm_carton,       wh_rm,  100)  # Available
    # The Cookie SFG has already been produced (its own sub-production run), so
    # 1,000 units are on hand in the Production warehouse — this lets the finished
    # Smarties Cookies order consume its two-level components and complete fully.
    set_stock(sfg_cookie,      wh_prod, 1000)  # produced SFG, ready for FG assembly
    set_stock(fg_smarties,     wh_fg,      0)  # to be filled after production
    log("Stock set: 11 materials available, Dates=30kg (short 20 kg intentionally), Cookie SFG=1000 produced")

    # ── 9. BOMs ────────────────────────────────────────────────────────────────
    # BOM-1: per 1 Cookie unit → proportional from 100-batch spec
    bom_cookie = build_bom(sfg_cookie, [
        (rm_white_flour,  0.50, "kg"),   # 50kg / 100 cookies
        (rm_brown_flour,  0.20, "kg"),
        (rm_sugar,        0.15, "kg"),
        (rm_coconut,      0.05, "kg"),
        (rm_dates,        0.05, "kg"),
        (rm_almonds,      0.05, "kg"),
        (rm_flavoring,    0.01, "kg"),
        (rm_art_colors,   0.005,"kg"),
    ])

    # BOM-2: per 1 Smarties Cookie → from 100-batch spec
    bom_smarties = build_bom(fg_smarties, [
        (sfg_cookie,   1.0,  "unit"),  # 1 cookie per finished good
        (rm_smarties,  0.10, "kg"),    # 10 kg / 100 pcs
        (rm_label,     1.0,  "unit"),
        (rm_bag,       1.0,  "unit"),
        (rm_carton,    0.10, "unit"),  # 10 cartons / 100 pcs
    ])
    log("BOM-1 (Cookie) and BOM-2 (Smarties Cookies) created")

    # ── 10. Production Lines ───────────────────────────────────────────────────
    line_cookie  = ProductionLine.objects.get_or_create(company=co, name="Cookie Production Line",
        defaults={"location": "Plant 1 - Hall A", "capacity": 200, "status": "running"})[0]
    line_packing = ProductionLine.objects.get_or_create(company=co, name="Packing Line",
        defaults={"location": "Plant 1 - Hall B", "capacity": 150, "status": "running"})[0]
    log(f"Lines: {line_cookie.name}, {line_packing.name}")

    # ── 11. Resources (Machines + Manpower) ───────────────────────────────────
    mixer     = get_or_create_resource(co, "Mixer-01",           "machine",   "Mixer",       line_cookie,  350, run_rate=200)
    oven      = get_or_create_resource(co, "Oven-01",            "machine",   "Oven",        line_cookie,  500, run_rate=150)
    cooling   = get_or_create_resource(co, "Cooling-Rack-01",    "machine",   "Cooling",     line_cookie,  100, run_rate=300)
    packer    = get_or_create_resource(co, "Packing-Machine-01", "machine",   "Packing",     line_packing, 600, run_rate=120)

    rahul  = get_or_create_resource(co, "Rahul (OP-001)",  "manpower", "Mixing Operator",   line_cookie,  80)
    priya  = get_or_create_resource(co, "Priya (OP-002)",  "manpower", "Baking Operator",   line_cookie,  80)
    ankit  = get_or_create_resource(co, "Ankit (OP-003)",  "manpower", "Cooling Operator",  line_cookie,  80)
    neha   = get_or_create_resource(co, "Neha (OP-004)",   "manpower", "Packing Operator",  line_packing, 80)
    log("Resources: Mixer-01, Oven-01, Cooling-Rack-01, Packing-Machine-01 + 4 operators")

    # ── 12. Recipes with Routing ───────────────────────────────────────────────
    recipe_cookie   = build_recipe(sfg_cookie, 100, line_cookie, [
        (1, "Mixing",  mixer,  rahul, 30, 0.10),
        (2, "Baking",  oven,   priya, 20, 0.15),
        (3, "Cooling", cooling, ankit, 10, 0.05),
    ])
    recipe_smarties = build_recipe(fg_smarties, 100, line_packing, [
        (1, "Smarties Addition", None,    neha,  15, 0.08),
        (2, "Packing",           packer,  neha,  20, 0.12),
        (3, "Final Inspection",  None,    neha,  10, 0.05),
    ])
    log("Recipes: Cookie (3-op routing) + Smarties Cookies (3-op routing)")

    # ── 13. Vendors ────────────────────────────────────────────────────────────
    v_flour  = get_or_create_vendor(co, "ABC Flour & Ingredients",     "purchase@abcflour.demo",    "+91-22-23456789", "Mumbai, Maharashtra", 4.5)
    v_dry    = get_or_create_vendor(co, "Premium Dry Fruits Supplier", "sales@premiumdryfruits.demo","+91-20-23456780", "Pune, Maharashtra",   4.3)
    v_food   = get_or_create_vendor(co, "Food Ingredients & Flavours Ltd.", "orders@foodflavours.demo", "+91-80-23456781", "Bengaluru, Karnataka", 4.6)
    v_smart  = get_or_create_vendor(co, "Smarties Confectionery Supplier",  "supply@smartiesco.demo",  "+91-11-23456782", "Delhi, NCR",          4.7)
    v_pack   = get_or_create_vendor(co, "PackRight Packaging Pvt. Ltd.",    "sales@packright.demo",    "+91-79-23456783", "Ahmedabad, Gujarat",  4.4)

    # Vendor price lists (INR)
    get_or_create_vendor_price(v_flour, rm_white_flour, 32,  500, 5)
    get_or_create_vendor_price(v_flour, rm_brown_flour, 38,  500, 5)
    get_or_create_vendor_price(v_flour, rm_sugar,       45,  500, 5)
    get_or_create_vendor_price(v_flour, rm_coconut,     80,  200, 7)
    get_or_create_vendor_price(v_dry,   rm_dates,      120,   50, 7)
    get_or_create_vendor_price(v_dry,   rm_almonds,    500,   50, 7)
    get_or_create_vendor_price(v_food,  rm_flavoring,  400,   25, 10)
    get_or_create_vendor_price(v_food,  rm_art_colors, 350,   10, 10)
    get_or_create_vendor_price(v_smart, rm_smarties,   200,   50, 7)
    get_or_create_vendor_price(v_pack,  rm_label,        2, 1000, 3)
    get_or_create_vendor_price(v_pack,  rm_bag,          5, 1000, 3)
    get_or_create_vendor_price(v_pack,  rm_carton,      30,  100, 3)
    log("Vendors: 5 suppliers with price lists")

    # ── 14. Customer & Sales Order ─────────────────────────────────────────────
    cust, _ = Customer.objects.get_or_create(
        company=co, name="ABC Retail Foods Pvt. Ltd.",
        defaults={
            "email": "procurement@abcretailfoods.demo",
            "phone": "+91-22-61234567",
            "address": "Andheri East, Mumbai, Maharashtra - 400069",
        }
    )

    # Customer Order CO-2026-001
    so_qs = SalesOrder.objects.filter(customer=cust, customer_order_reference="ABC-ORD-4587")
    if so_qs.exists():
        so = so_qs.first()
    else:
        so = SalesOrder.objects.create(
            customer=cust,
            status="confirmed",
            source="email",
            required_delivery_date=date(2026, 11, 30),
            customer_order_reference="ABC-ORD-4587",
            priority="high",
            custom_specifications="Smarties Cookies – retail packing, halal certified",
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            item=fg_smarties,
            quantity=1000,
            unit_price=Decimal("700"),
        )
        so.total_amount = Decimal("700000")
        so.save(update_fields=["total_amount"])

    so_item = so.salesorderitem_set.first()
    log(f"Sales Order: SO-{so.id} (ref: ABC-ORD-4587)  ₹7,00,000  → customer: {cust.name}")

    # ── 15. Production Plan ────────────────────────────────────────────────────
    plan_qs = ProductionPlan.objects.filter(sales_order=so, item=fg_smarties)
    if plan_qs.exists():
        plan = plan_qs.first()
    else:
        plan = ProductionPlan(
            company=co,
            sales_order=so,
            sales_order_item=so_item,
            customer=cust,
            item=fg_smarties,
            order_quantity=1000,
            planned_quantity=1000,
            target_date=date(2026, 11, 21),
            status="planned",
            notes="MTO Demo - CO-2026-001. Two-level BOM: Cookie SFG + Smarties Cookies FG.",
            created_by=admin_user,
        )
        plan.save()   # triggers auto plan_number
        plan.plan_number = "PP-2026-001"
        plan.save(update_fields=["plan_number"])

    log(f"Production Plan: {plan.plan_number} → status={plan.status}")

    # ── 16. Purchase Requisition for Dates shortage ────────────────────────────
    pr_qs = PurchaseRequisition.objects.filter(production_plan=plan)
    if pr_qs.exists():
        pr = pr_qs.first()
    else:
        pr = PurchaseRequisition(
            company=co,
            production_plan=plan,
            sales_order=so,
            warehouse=wh_rm,
            status="converted",
            notes="MRP shortage: Dates 20 kg. Required for CO-2026-001.",
            created_by=admin_user,
        )
        pr.save()
        pr.requisition_number = "PR-2026-001"
        pr.save(update_fields=["requisition_number"])
        PurchaseRequisitionItem.objects.create(
            requisition=pr,
            item=rm_dates,
            required_quantity=50,
            available_quantity=30,
            shortage_quantity=20,
            uom="kg",
            estimated_unit_price=Decimal("120"),
        )

    log(f"Purchase Requisition: {pr.requisition_number}  Dates 50kg req / 30kg avail / 20kg short")

    # ── 17. Purchase Order PO-2026-001 ─────────────────────────────────────────
    po_qs = PurchaseOrder.objects.filter(vendor=v_dry, requisition=pr)
    if po_qs.exists():
        po = po_qs.first()
    else:
        po = PurchaseOrder.objects.create(
            vendor=v_dry,
            requisition=pr,
            status="received",
            expected_delivery=date(2026, 10, 15),
            priority="high",
            notes="Urgently required for CO-2026-001 / PP-2026-001.",
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            item=rm_dates,
            quantity=20,
            unit_price=Decimal("120"),
        )
        po.recalculate_total()

    log(f"Purchase Order: PO-{po.id}  Dates 20kg × ₹120  → {v_dry.name}  status={po.status}")

    # ── 18. Goods Receipt ──────────────────────────────────────────────────────
    gr_qs = GoodsReceipt.objects.filter(purchase_order=po, warehouse=wh_rm)
    if not gr_qs.exists():
        GoodsReceipt.objects.create(purchase_order=po, warehouse=wh_rm)
        # Add received quantity to stock
        stock_dates = Stock.objects.get(item=rm_dates, warehouse=wh_rm)
        stock_dates.quantity += 20   # now 30 + 20 = 50 kg
        stock_dates.save(update_fields=["quantity"])
        StockMovement.objects.create(
            item=rm_dates, warehouse=wh_rm,
            movement_type="IN", quantity=20,
            reference=f"GRN against PO-{po.id} / {pr.requisition_number}",
        )

    log(f"Goods Receipt: 20 kg Dates → {wh_rm.name}  (Dates stock now 50 kg)")

    # ── 19. Incoming QC for Dates ──────────────────────────────────────────────
    gr = GoodsReceipt.objects.filter(purchase_order=po, warehouse=wh_rm).first()
    iqc_qs = IncomingQualityCheck.objects.filter(purchase_order=po, item=rm_dates)
    if not iqc_qs.exists() and gr:
        iqc = IncomingQualityCheck(
            company=co,
            goods_receipt=gr,
            purchase_order=po,
            vendor=v_dry,
            item=rm_dates,
            received_warehouse=wh_rm,
            destination_warehouse=wh_rm,
            received_quantity=20,
            released_quantity=20,
            uom="kg",
            status="passed",
            inspector=qual_user,
            parameters={
                "moisture": {"target": "<20%", "result": "15%", "pass": True},
                "mould": {"target": "Absent", "result": "Absent", "pass": True},
                "colour": {"target": "Dark brown", "result": "Dark brown", "pass": True},
            },
            remarks="Dates 20 kg received from Premium Dry Fruits Supplier. All parameters within spec. PASS — released to Raw Material Warehouse for production (CO-2026-001).",
        )
        iqc.save()
        iqc.check_number = f"IQC-2026-001"
        iqc.save(update_fields=["check_number"])
    log("Incoming QC: IQC-2026-001  Dates 20 kg — PASS → released to Raw Material Warehouse")

    # ── 20. Production Order ───────────────────────────────────────────────────
    prod_qs = ProductionOrder.objects.filter(production_plan=plan)
    if prod_qs.exists():
        prod = prod_qs.first()
    else:
        prod = ProductionOrder.objects.create(
            recipe=recipe_smarties,
            quantity=1000,
            warehouse=wh_prod,
            line=line_packing,
            sales_order=so,
            production_plan=plan,
            production_plan_ref=plan.plan_number,
            status="running",
            produced_quantity=0,
            planned_start=timezone.make_aware(datetime(2026, 11, 16, 8, 0)),
            planned_end=timezone.make_aware(datetime(2026, 11, 21, 17, 0)),
            notes="MTO Production Order for CO-2026-001. Two-level BOM: Cookie SFG first, then Smarties Cookies.",
        )
        prod.order_number = "PRD-2026-001"
        prod.save(update_fields=["order_number"])

        # Material requirements
        for rm, req_qty in [
            (rm_white_flour,  500), (rm_brown_flour, 200), (rm_sugar,       150),
            (rm_coconut,       50), (rm_dates,         50), (rm_almonds,      50),
            (rm_flavoring,     10), (rm_art_colors,     5), (rm_smarties,    100),
            (rm_label,       1000), (rm_bag,          1000), (rm_carton,      100),
        ]:
            avail = Stock.objects.filter(item=rm, warehouse=wh_rm).first()
            avail_qty = avail.quantity if avail else 0
            short = max(req_qty - avail_qty, 0)
            ProductionMaterialRequirement.objects.get_or_create(
                production_order=prod, item=rm,
                defaults={
                    "required_quantity": req_qty,
                    "reserved_quantity": min(avail_qty, req_qty),
                    "shortage_quantity": short,
                }
            )

        # Also record intermediate Cookie requirement
        ProductionMaterialRequirement.objects.get_or_create(
            production_order=prod, item=sfg_cookie,
            defaults={
                "required_quantity": 1000,
                "reserved_quantity": 0,
                "shortage_quantity": 1000,  # produced internally
            }
        )

    log(f"Production Order: {prod.order_number}  qty=1,000  status={prod.status}")

    # Update plan status to converted
    plan.status = "converted"
    plan.save(update_fields=["status"])

    # ── 21. Production Operations (WIP state: Mixing ✓, Baking ✓, Cooling 70%) ─
    ops_data = [
        # (seq, name, machine, manpower, planned_qty, completed_qty, status,
        #  planned_start, planned_end, actual_start, actual_end)
        (1, "Mixing",           mixer,   rahul,  1000, 1000, "completed",
         datetime(2026, 11, 16,  8, 0), datetime(2026, 11, 16, 17, 0),
         datetime(2026, 11, 16,  8, 0), datetime(2026, 11, 16, 16, 30)),
        (2, "Baking",           oven,    priya,  1000, 1000, "completed",
         datetime(2026, 11, 17,  8, 0), datetime(2026, 11, 17, 17, 0),
         datetime(2026, 11, 17,  8, 0), datetime(2026, 11, 17, 16, 45)),
        (3, "Cooling",          cooling, ankit,  1000,  700, "in_progress",
         datetime(2026, 11, 18,  8, 0), datetime(2026, 11, 18, 17, 0),
         datetime(2026, 11, 18,  8, 0), None),
        (4, "Smarties Addition",None,    neha,   1000,    0, "not_started",
         datetime(2026, 11, 19,  8, 0), datetime(2026, 11, 19, 14, 0), None, None),
        (5, "Packing",          packer,  neha,   1000,    0, "not_started",
         datetime(2026, 11, 20,  8, 0), datetime(2026, 11, 20, 17, 0), None, None),
        (6, "Final Inspection", None,    neha,   1000,    0, "not_started",
         datetime(2026, 11, 21,  8, 0), datetime(2026, 11, 21, 12, 0), None, None),
    ]

    for seq, name, machine, manpower, pq, cq, status, ps, pe, as_, ae in ops_data:
        op, _ = ProductionOperation.objects.get_or_create(
            production_order=prod, sequence=seq,
            defaults={
                "name": name,
                "machine": machine,
                "manpower": manpower,
                "operator": manpower.name if manpower else "",
                "planned_quantity": pq,
                "started_quantity": cq,
                "completed_quantity": cq,
                "status": status,
                "planned_start": timezone.make_aware(ps),
                "planned_end": timezone.make_aware(pe),
                "actual_start": timezone.make_aware(as_) if as_ else None,
                "actual_end": timezone.make_aware(ae) if ae else None,
            }
        )

    prod.produced_quantity = 0  # WIP – not yet completed
    prod.status = "running"
    prod.save(update_fields=["produced_quantity", "status"])
    log("Operations: Mixing 1000/1000 ✓ | Baking 1000/1000 ✓ | Cooling 700/1000 ● | Packing 0/1000 ○ | Inspection 0/1000 ○")
    log("WIP = 300 units in cooling (in_progress)")

    # An earlier sub-batch of 300 units has already finished the full routing,
    # passed FG QA and been moved into the Finished Goods warehouse. This lets
    # the demo show the *complete* downstream chain (FG stock → allocation →
    # dispatch/Delivery Note → invoice → AR) as a PARTIAL fulfilment, while the
    # remaining 700 are still the WIP shown above. This demonstrates req #50
    # (partial production & dispatch) end to end.
    DISPATCHED_QTY = 300

    # ── 22. FG QA + Finished-Goods stock for the completed sub-batch ───────────
    fgqc_qs = QualityCheck.objects.filter(item=fg_smarties, production_order=prod)
    if not fgqc_qs.exists():
        QualityCheck.objects.create(
            production_order=prod,
            item=fg_smarties,
            inspector=qual_user,
            inspection_type="production",
            status="approved",
            result_decision="pass",
            received_quantity=DISPATCHED_QTY,
            accepted_quantity=DISPATCHED_QTY,
            rejected_quantity=0,
            remarks=("FG QA gate (req #28): 300-unit sub-batch of Smarties Cookies "
                     "passed final inspection — released to Finished Goods Warehouse."),
        )
    set_stock(fg_smarties, wh_fg, DISPATCHED_QTY)
    StockMovement.objects.get_or_create(
        item=fg_smarties, warehouse=wh_fg, movement_type="IN", quantity=DISPATCHED_QTY,
        reference=f"FG production receipt — {prod.order_number} (QA passed)",
    )
    log(f"FG QA passed + {DISPATCHED_QTY} units Smarties Cookies → Finished Goods Warehouse")

    # ── 23. FG Allocation against the customer order (req #25) ─────────────────
    alloc_qs = FGAllocation.objects.filter(sales_order=so, item=fg_smarties)
    if alloc_qs.exists():
        alloc = alloc_qs.first()
    else:
        alloc = FGAllocation.objects.create(
            company=co,
            sales_order=so,
            sales_order_item=so_item,
            item=fg_smarties,
            quantity=DISPATCHED_QTY,
            dispatched_quantity=DISPATCHED_QTY,
            status="dispatched",
            created_by=store_user,
        )
    log(f"FG Allocation: {DISPATCHED_QTY} units reserved for {cust.name} against SO-{so.id}")

    # ── 24. Dispatch + Delivery Note with logistics (req #29, #30, #38) ────────
    dsp_qs = Dispatch.objects.filter(sales_order=so)
    if dsp_qs.exists():
        dsp = dsp_qs.first()
    else:
        dsp = Dispatch.objects.create(
            company=co,
            sales_order=so,
            warehouse=wh_fg,
            status="dispatched",
            shipment_mode="road",
            carrier="BlueDart Surface Logistics",
            shipment_reference="LR-MH-2026-88214",
            vehicle_number="MH-04-GT-5521",
            driver_name="Ramesh Kumar",
            freight_amount=Decimal("4500"),
            packages=30,
            gross_weight="340 kg",
            ship_to_name=cust.name,
            ship_to_address=cust.address,
            expected_delivery=date(2026, 11, 25),
            delivery_notes=("Partial dispatch #1 of CO-2026-001: 300 of 1,000 units. "
                            "Balance 700 to follow on completion of production."),
            verified_by=store_user,
            verified_at=timezone.make_aware(datetime(2026, 11, 22, 11, 0)),
            dispatched_at=timezone.make_aware(datetime(2026, 11, 22, 15, 30)),
            created_by=store_user,
        )
        DispatchLine.objects.create(
            dispatch=dsp,
            sales_order_item=so_item,
            item=fg_smarties,
            requested_quantity=1000,
            quantity=DISPATCHED_QTY,
            packed=True,
            unit_price=Decimal("700"),
        )
        # Finished goods leave the FG warehouse on dispatch.
        StockMovement.objects.create(
            item=fg_smarties, warehouse=wh_fg, movement_type="OUT",
            quantity=DISPATCHED_QTY,
            reference=f"Dispatch {dsp.dispatch_number} → {cust.name}",
        )
        set_stock(fg_smarties, wh_fg, 0)
    log(f"Dispatch: {dsp.dispatch_number}  {DISPATCHED_QTY} units by Road ({dsp.carrier}, {dsp.vehicle_number})")

    # ── 25. Invoice INV-2026-001 (for the dispatched 300 units) ────────────────
    inv_amount = Decimal("700") * DISPATCHED_QTY  # ₹2,10,000
    inv_qs = Invoice.objects.filter(sales_order=so, company=co)
    if inv_qs.exists():
        inv = inv_qs.first()
    else:
        inv = Invoice.objects.create(
            company=co,
            sales_order=so,
            customer=cust,
            invoice_date=date(2026, 11, 22),
            due_date=date(2026, 12, 22),
            total_amount=inv_amount,
            amount_paid=Decimal("0"),
            status="open",
        )
        InvoiceLine.objects.create(
            invoice=inv,
            sales_order_item=so_item,
            item=fg_smarties,
            description=f"Smarties Cookies — {DISPATCHED_QTY} units @ ₹700 (partial dispatch 1)",
            quantity=DISPATCHED_QTY,
            unit_price=Decimal("700"),
            amount=inv_amount,
        )
        dsp.invoice = inv
        dsp.save(update_fields=["invoice"])
    log(f"Invoice: INV-{inv.id}  ₹{inv_amount:,.0f}  (300 units dispatched)  due=22 Dec 2026")

    # ── 26. Accounts Receivable — partial customer payment (req #32) ───────────
    pay_qs = CustomerPayment.objects.filter(invoice=inv)
    if not pay_qs.exists():
        part_payment = Decimal("100000")  # ₹1,00,000 part payment → balance ₹1,10,000
        CustomerPayment.objects.create(
            company=co,
            customer=cust,
            invoice=inv,
            amount=part_payment,
            payment_date=date(2026, 11, 28),
            method="bank_transfer",
            reference="NEFT-ABCRETAIL-88213",
        )
        inv.apply_payment(part_payment)
    inv.refresh_from_db()
    log(f"AR: ₹1,00,000 received  →  INV-{inv.id} status={inv.status}, balance ₹{inv.balance_due:,.0f}")

    # ══ SUMMARY ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*65}")
    print("  DEMO COMPANY READY")
    print(f"{'═'*65}")
    print(f"  Company     : {COMPANY_NAME}")
    print(f"  {'─'*60}")
    print(f"  LOGIN CREDENTIALS")
    print(f"  Password    : {PASSWORD}")
    print(f"  Admin       : admin@{COMPANY_SLUG}   (admin@smartiesfoods.com)")
    print(f"  Production  : production@{COMPANY_SLUG}")
    print(f"  Quality     : quality@{COMPANY_SLUG}")
    print(f"  Store       : store@{COMPANY_SLUG}")
    print(f"  {'─'*60}")
    print(f"  MTO DEMO CHAIN")
    print(f"  Customer Order  : SO-{so.id}  (ref ABC-ORD-4587)  ₹7,00,000")
    print(f"  Production Plan : {plan.plan_number}")
    print(f"  BOM-1 (Cookie)  : White Flour, Brown Flour, Sugar, Coconut,")
    print(f"                    Dates(SHORT), Almonds, Flavoring, Artif.Colors")
    print(f"  BOM-2 (Smarties): Cookie + Smarties + Label + Bag + Carton")
    print(f"  Purchase Req    : {pr.requisition_number}  Dates 20 kg short")
    print(f"  Purchase Order  : PO-{po.id}  Dates 20 kg @ ₹120  → received")
    print(f"  Production Order: {prod.order_number}  1,000 units  status=running")
    print(f"  FG Allocation   : {int(DISPATCHED_QTY)} units reserved for {cust.name}")
    print(f"  Dispatch        : {dsp.dispatch_number}  {int(DISPATCHED_QTY)} units by Road ({dsp.carrier})")
    print(f"  Invoice         : INV-{inv.id}  ₹{inv.total_amount:,.0f}  status={inv.status}")
    print(f"  AR Payment      : ₹{inv.amount_paid:,.0f} received, balance ₹{inv.balance_due:,.0f}")
    print(f"  {'─'*60}")
    print(f"  WIP STATUS (remaining 700 units in production)")
    print(f"  Mixing     1000/1000  ✓ Completed")
    print(f"  Baking     1000/1000  ✓ Completed")
    print(f"  Cooling     700/1000  ● In Progress (WIP=300)")
    print(f"  Packing       0/1000  ○ Not Started")
    print(f"  Inspection    0/1000  ○ Not Started")
    print(f"  {'─'*60}")
    print(f"  PARTIAL FULFILMENT: 300 of 1,000 units completed → QA → FG →")
    print(f"  allocated → dispatched (Delivery Note) → invoiced → part-paid.")
    print(f"  Balance 700 units still in production (demonstrates req #50).")
    print(f"{'═'*65}\n")


if __name__ == "__main__":
    seed()
