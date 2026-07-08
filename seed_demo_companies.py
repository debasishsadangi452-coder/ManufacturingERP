"""
Seed two industry demo companies into the ERP via the public API.

  1. ApexForge Metals    - aluminium extrusion, springs & wire products,
                           fabricated metal, general purpose machinery
  2. PureSweet Naturals  - plant-based sweeteners (agave, monk fruit,
                           erythritol, xylitol, coconut sugar, date syrup)

Run: python seed_demo_companies.py
Target another instance with: ERP_API_BASE=http://127.0.0.1:8000/api python seed_demo_companies.py
"""
import os
import requests

BASE = os.getenv("ERP_API_BASE", "https://manufacturingerp-production.up.railway.app/api")
PASSWORD = "demo1234"


class Client:
    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}"}

    def get(self, path):
        r = requests.get(f"{BASE}{path}", headers=self.h)
        return r.json() if r.status_code == 200 else []

    def post(self, path, data, quiet=False):
        r = requests.post(f"{BASE}{path}", json=data, headers=self.h)
        if r.status_code not in (200, 201) and not quiet:
            print(f"  ! POST {path} -> {r.status_code}: {r.text[:150]}")
        return r.json() if r.status_code in (200, 201) else None


def register_company(company_name, first_name, email):
    r = requests.post(f"{BASE}/auth/company/register/", json={
        "company_name": company_name,
        "first_name": first_name,
        "email": email,
        "password": PASSWORD,
    })
    if r.status_code == 201:
        print(f"Registered company '{company_name}' (admin {email})")
        return r.json()["access"]
    # Already registered -> log in with the admin email
    r = requests.post(f"{BASE}/token/", json={"username": email, "password": PASSWORD})
    if r.status_code == 200:
        print(f"Company '{company_name}' already exists, logged in as {email}")
        return r.json()["access"]
    raise SystemExit(f"Cannot register or log in for {company_name}: {r.text[:200]}")


def seed_company(spec):
    token = register_company(spec["company"], spec["admin_first"], spec["admin_email"])
    c = Client(token)

    c.post("/auth/subscription/select/", {"plan": "premium_ai"}, quiet=True)
    c.post("/auth/subscription/complete-onboarding/", {}, quiet=True)

    # --- Warehouses ---
    existing_wh = {w["name"]: w["id"] for w in c.get("/inventory/warehouses/")}
    wh = {}
    for name, loc in spec["warehouses"]:
        if name in existing_wh:
            wh[name] = existing_wh[name]
        else:
            r = c.post("/inventory/warehouses/", {"name": name, "location": loc})
            if r:
                wh[name] = r["id"]
    print(f"  Warehouses: {list(wh)}")

    # --- Items (raw materials + finished goods, with opening stock) ---
    existing_items = {i["name"]: i["id"] for i in c.get("/inventory/items/")}
    items = {}
    for row in spec["items"]:
        name, category, unit, wh_name, qty = row[:5]
        price = row[5] if len(row) > 5 else 0
        if name in existing_items:
            items[name] = existing_items[name]
            continue
        r = c.post("/inventory/items/", {
            "name": name, "category": category, "unit": unit, "selling_price": price,
            "warehouse_id": wh[wh_name], "initial_quantity": qty,
        })
        if r:
            items[name] = r["id"]
    print(f"  Items: {len(items)}")

    # --- Vendors + price lists ---
    existing_vendors = {v["name"]: v["id"] for v in c.get("/procurement/vendors/")}
    vendors = {}
    for v in spec["vendors"]:
        if v["name"] in existing_vendors:
            vendors[v["name"]] = existing_vendors[v["name"]]
        else:
            r = c.post("/procurement/vendors/", v)
            if r:
                vendors[v["name"]] = r["id"]
    for vendor_name, item_name, price, moq, lead in spec["vendor_prices"]:
        if vendor_name in vendors and item_name in items:
            c.post("/procurement/vendor-prices/", {
                "vendor": vendors[vendor_name], "item": items[item_name],
                "unit_price": price, "min_order_qty": moq, "lead_time_days": lead,
            }, quiet=True)
    print(f"  Vendors: {list(vendors)}")

    # --- Customers ---
    existing_cust = {x["name"]: x["id"] for x in c.get("/sales/customers/")}
    customers = {}
    for cu in spec["customers"]:
        if cu["name"] in existing_cust:
            customers[cu["name"]] = existing_cust[cu["name"]]
        else:
            r = c.post("/sales/customers/", cu)
            if r:
                customers[cu["name"]] = r["id"]
    print(f"  Customers: {list(customers)}")

    # --- Production lines ---
    existing_lines = {l["name"]: l["id"] for l in c.get("/production/lines/")}
    for name, loc, cap in spec["lines"]:
        if name not in existing_lines:
            c.post("/production/lines/", {"name": name, "location": loc, "capacity": cap})
    print(f"  Lines: {[l[0] for l in spec['lines']]}")

    # --- Recipes (BOM) ---
    existing_recipes = {r["product"] for r in c.get("/production/recipes/")}
    for product_name, ingredients in spec["recipes"]:
        if product_name not in items or items[product_name] in existing_recipes:
            continue
        r = c.post("/production/recipes/", {"product": items[product_name]})
        if r:
            for ing_name, qty in ingredients:
                if ing_name in items:
                    c.post("/production/recipe-ingredients/", {
                        "recipe": r["id"], "item": items[ing_name], "quantity": qty,
                    }, quiet=True)
    print(f"  Recipes: {len(spec['recipes'])}")

    # --- Purchase orders ---
    existing_po = {(p["vendor_name"], p["status"]) for p in c.get("/procurement/purchase-orders/")}
    for vendor_name, status, lines in spec["purchase_orders"]:
        if vendor_name not in vendors or (vendor_name, status) in existing_po:
            continue
        po = c.post("/procurement/purchase-orders/", {"vendor": vendors[vendor_name], "status": status})
        if po:
            for item_name, qty, price in lines:
                if item_name in items:
                    c.post("/procurement/purchase-order-items/", {
                        "purchase_order": po["id"], "item": items[item_name],
                        "quantity": qty, "unit_price": price,
                    }, quiet=True)
    print(f"  Purchase orders: {len(spec['purchase_orders'])}")

    # --- Sales orders ---
    existing_so = {(s["customer_name"], s["status"]) for s in c.get("/sales/sales-orders/")}
    for cust_name, status, so_lines in spec["sales_orders"]:
        if cust_name not in customers or (cust_name, status) in existing_so:
            continue
        c.post("/sales/sales-orders/", {
            "customer": customers[cust_name], "status": status,
            "items": [{"item": items[n], "quantity": q} for n, q in so_lines if n in items],
        })
    print(f"  Sales orders: {len(spec['sales_orders'])}")

    # --- Production orders (manufacturing jobs on the lines) ---
    recipe_by_product = {r["product_name"]: r["id"] for r in c.get("/production/recipes/")}
    line_by_name = {l["name"]: l["id"] for l in c.get("/production/lines/")}
    existing_prod = {(o["recipe_name"], o["status"]) for o in c.get("/production/production-orders/")}
    for product_name, qty, wh_name, line_name, status in spec.get("production_orders", []):
        if (product_name, status) in existing_prod:
            continue
        if product_name in recipe_by_product and line_name in line_by_name:
            c.post("/production/production-orders/", {
                "recipe": recipe_by_product[product_name], "quantity": qty,
                "warehouse": wh[wh_name], "line": line_by_name[line_name],
                "status": status,
            })
    print(f"  Production orders: {len(spec.get('production_orders', []))}")

    # --- Batches (lot tracking, mainly for food) ---
    for item_name, batch_no, expiry, qty in spec.get("batches", []):
        if item_name in items:
            c.post("/inventory/batches/", {
                "item": items[item_name], "batch_number": batch_no,
                "expiry_date": expiry, "quantity": qty,
            }, quiet=True)

    seed_logistics(c, spec)
    seed_workforce_and_finance(c, spec)
    seed_ai_scenarios(c, spec, items, wh, customers)
    print("  Done.\n")


def seed_ai_scenarios(c, spec, items, wh, customers):
    """Data behind the AI demo storylines: predictive maintenance, quality
    rejection trends, low-stock procurement, and dormant customers."""
    line_by_name = {l["name"]: l["id"] for l in c.get("/production/lines/")}

    # Equipment with health scores (AI Maintenance: 'Machine X will fail soon')
    existing_eq = {e["name"]: e["id"] for e in c.get("/maintenance/equipment/")}
    equipment = {}
    for name, line, status, health, uptime, last_m, next_m in spec.get("equipment", []):
        if name in existing_eq:
            equipment[name] = existing_eq[name]
        elif line in line_by_name:
            r = c.post("/maintenance/equipment/", {
                "name": name, "line": line_by_name[line], "status": status,
                "health": health, "uptime": uptime,
                "last_maintenance": last_m, "next_maintenance": next_m,
            })
            if r:
                equipment[name] = r["id"]

    existing_tasks = {(t["equipment"], t["task_type"]) for t in c.get("/maintenance/tasks/")}
    for eq_name, ttype, status, prio, sched, dur, desc, tech, amount in spec.get("maintenance_tasks", []):
        eq_id = equipment.get(eq_name)
        if eq_id and (eq_id, ttype) not in existing_tasks:
            c.post("/maintenance/tasks/", {
                "equipment": eq_id, "task_type": ttype, "status": status,
                "priority": prio, "scheduled_date": sched, "estimated_duration": dur,
                "description": desc, "technician_name": tech, "amount": amount,
            })
    print(f"  Maintenance: {len(equipment)} equipment, {len(spec.get('maintenance_tasks', []))} tasks")

    # Quality checks (AI Quality: rejection trends per line)
    prod_orders = c.get("/production/production-orders/")
    po_by_recipe = {}
    for o in prod_orders:
        po_by_recipe.setdefault(o["recipe_name"], o["id"])
    existing_qc = {(q["production_order"], q["parameter"]) for q in c.get("/quality/quality-checks/")}
    for recipe_name, status, test_type, parameter, result, target, remarks in spec.get("quality_checks", []):
        po_id = po_by_recipe.get(recipe_name)
        if po_id and (po_id, parameter) not in existing_qc:
            c.post("/quality/quality-checks/", {
                "production_order": po_id, "status": status, "test_type": test_type,
                "parameter": parameter, "result": result, "target": target, "remarks": remarks,
            })
    print(f"  Quality: {len(spec.get('quality_checks', []))} checks")

    # Low stock (AI Procurement: below minimum -> recommend supplier -> PO)
    stock_now = {}
    for s in c.get("/inventory/stock/"):
        stock_now[(s.get("item"), s.get("warehouse"))] = s.get("quantity")
    for item_name, wh_name, new_qty in spec.get("low_stock", []):
        if item_name in items and wh_name in wh:
            key = (items[item_name], wh[wh_name])
            if stock_now.get(key) != new_qty:
                c.post("/inventory/stock/adjust/", {
                    "item": items[item_name], "warehouse": wh[wh_name], "quantity": new_qty,
                })
    print(f"  Low-stock items set: {[r[0] for r in spec.get('low_stock', [])]}")

    # Dormant customer order (AI Sales: 'who hasn't ordered in 90 days?')
    # Created now, then backdated at the DB level by the caller.
    existing_so = {(s["customer_name"], s["status"]) for s in c.get("/sales/sales-orders/")}
    for cust_name, status, so_lines in spec.get("stale_orders", []):
        if cust_name in customers and (cust_name, status) not in existing_so:
            c.post("/sales/sales-orders/", {
                "customer": customers[cust_name], "status": status,
                "items": [{"item": items[n], "quantity": q} for n, q in so_lines if n in items],
            })


def seed_logistics(c, spec):
    existing_drivers = {d["license_number"]: d["id"] for d in c.get("/logistics/drivers/")}
    drivers = {}
    for d in spec.get("drivers", []):
        if d["license_number"] in existing_drivers:
            drivers[d["name"]] = existing_drivers[d["license_number"]]
        else:
            r = c.post("/logistics/drivers/", d)
            if r:
                drivers[d["name"]] = r["id"]

    existing_vehicles = {v["name"]: v["id"] for v in c.get("/logistics/vehicles/")}
    vehicles = {}
    for v in spec.get("vehicles", []):
        if v["name"] in existing_vehicles:
            vehicles[v["name"]] = existing_vehicles[v["name"]]
        else:
            r = c.post("/logistics/vehicles/", v)
            if r:
                vehicles[v["name"]] = r["id"]

    existing_routes = {r["name"]: r["id"] for r in c.get("/logistics/routes/")}
    routes = {}
    for name, stops, dist, eta, veh, status, eff in spec.get("routes", []):
        if name in existing_routes:
            routes[name] = existing_routes[name]
        else:
            r = c.post("/logistics/routes/", {
                "name": name, "stops": stops, "distance": dist, "estimated_time": eta,
                "assigned_vehicle": vehicles.get(veh), "status": status, "efficiency": eff,
            })
            if r:
                routes[name] = r["id"]

    existing_ship = {(s["customer"], s["status"]) for s in c.get("/logistics/shipments/")}
    for cust, dest, status, prio, drv, veh, route, progress in spec.get("shipments", []):
        if (cust, status) in existing_ship:
            continue
        c.post("/logistics/shipments/", {
            "customer": cust, "destination": dest, "status": status, "priority": prio,
            "driver": drivers.get(drv), "vehicle": vehicles.get(veh),
            "route": routes.get(route), "progress": progress,
        })
    print(f"  Logistics: {len(drivers)} drivers, {len(vehicles)} vehicles, "
          f"{len(routes)} routes, {len(spec.get('shipments', []))} shipments")


def seed_workforce_and_finance(c, spec):
    # Departments are get-or-create by name (name/code globally unique)
    existing_depts = {d["name"]: d["id"] for d in c.get("/workforce/departments/")}
    depts = {}
    for name, code in spec.get("departments", []):
        if name in existing_depts:
            depts[name] = existing_depts[name]
        else:
            r = c.post("/workforce/departments/", {"name": name, "code": code}, quiet=True)
            if r:
                depts[name] = r["id"]

    existing_emp = {e["email"]: e["id"] for e in c.get("/workforce/employees/")}
    employees = {}
    for e in spec.get("employees", []):
        if e["email"] in existing_emp:
            employees[e["email"]] = existing_emp[e["email"]]
            continue
        payload = dict(e)
        payload["department"] = depts.get(payload.pop("dept", None))
        r = c.post("/workforce/employees/", payload)
        if r:
            employees[e["email"]] = r["id"]

    existing_shifts = {s["name"] for s in c.get("/workforce/shifts/")}
    for name, stype, start, end, cap in spec.get("shifts", []):
        if name not in existing_shifts:
            c.post("/workforce/shifts/", {
                "name": name, "shift_type": stype,
                "start_time": start, "end_time": end, "capacity": cap,
            }, quiet=True)

    # Today's attendance for the first few employees
    from datetime import date
    existing_att = {(a["employee"], a["date"]) for a in c.get("/workforce/attendance/")}
    for email in list(employees)[:4]:
        emp_id = employees[email]
        if (emp_id, str(date.today())) not in existing_att:
            c.post("/workforce/attendance/", {
                "employee": emp_id, "date": str(date.today()),
                "status": "present", "working_hours": 8,
            }, quiet=True)
    print(f"  Workforce: {len(depts)} departments, {len(employees)} employees")

    # --- Finance ---
    existing_budgets = {(b["department"], b["period_label"]) for b in c.get("/finance/budgets/")}
    for dept, period, label, total, auto_limit in spec.get("budgets", []):
        if (dept, label) not in existing_budgets:
            c.post("/finance/budgets/", {
                "department": dept, "period": period, "period_label": label,
                "total_budget": total, "auto_approve_limit": auto_limit,
            })

    existing_exp = {e["title"] for e in c.get("/finance/expenses/")}
    for row in spec.get("expenses", []):
        title, category, amount, vendor = row[:4]
        final_status = row[4] if len(row) > 4 else "pending"
        if title in existing_exp:
            continue
        r = c.post("/finance/expenses/", {
            "title": title, "category": category, "amount": amount, "vendor": vendor,
        })
        if r and final_status in ("approved", "rejected") and r.get("status") == "pending":
            c.post(f"/finance/expenses/{r['id']}/approve/", {
                "status": final_status,
                "notes": "Reviewed in monthly finance meeting." if final_status == "approved" else "Deferred to next quarter budget.",
            }, quiet=True)

    existing_costs = {o["title"] for o in c.get("/finance/operational-costs/")}
    for title, ctype, dept, amount, dt, vendor in spec.get("operational_costs", []):
        if title not in existing_costs:
            c.post("/finance/operational-costs/", {
                "title": title, "cost_type": ctype, "department": dept,
                "amount": amount, "date": dt, "vendor": vendor,
            })

    # Workforce payroll: per workforce Employee, keyed by month/year
    existing_pay = {(p["employee"], p["month"], p["year"]) for p in c.get("/workforce/payroll/")}
    for email, _label, basic, allow, tax in spec.get("payroll", []):
        emp_id = employees.get(email)
        if emp_id and (emp_id, 6, 2026) not in existing_pay:
            c.post("/workforce/payroll/", {
                "employee": emp_id, "month": 6, "year": 2026,
                "basic_salary": basic, "allowances": allow, "deductions": tax,
                "overtime_pay": 0, "net_pay": basic + allow - tax,
                "total_working_hours": 192,
            })

    # Finance payroll links to auth Users - monthly records for the company admin
    me = c.get("/auth/profile/")
    admin_salary = spec.get("admin_salary", 90000)
    if me and me.get("id"):
        existing_fin_pay = {(p["employee"], p["period_label"]) for p in c.get("/finance/payroll/")}
        for label, pay_status, pay_date in [
            ("Apr 2026", "paid", "2026-05-01"),
            ("May 2026", "paid", "2026-06-01"),
            ("Jun 2026", "paid", "2026-07-01"),
            ("Jul 2026", "pending", None),
        ]:
            if (me["id"], label) not in existing_fin_pay:
                c.post("/finance/payroll/", {
                    "employee": me["id"], "period_label": label,
                    "basic_salary": admin_salary, "allowances": 15000, "tax": 12000,
                    "deductions": 0, "overtime_pay": 0, "pay_status": pay_status,
                    "payment_date": pay_date,
                })
    print(f"  Finance: {len(spec.get('budgets', []))} budgets, {len(spec.get('expenses', []))} expenses, "
          f"{len(spec.get('operational_costs', []))} costs, {len(spec.get('payroll', []))} workforce payroll records")


# =====================================================================
# Company 1: ApexForge Metals
# Spring & wire products, fabricated metal, aluminium extrusion,
# general purpose machinery components.
# =====================================================================
RM_METAL = "Raw Material Store - Plant 1"
FG_METAL = "Finished Goods Warehouse - Plant 1"

APEXFORGE = {
    "company": "ApexForge Metals",
    "slug": "apexforgemetals",
    "admin_first": "Rajesh",
    "admin_email": "admin@apexforge.com",
    "warehouses": [
        (RM_METAL, "MIDC Bhosari, Pune"),
        (FG_METAL, "MIDC Bhosari, Pune"),
        ("Dispatch Yard - Plant 1", "MIDC Bhosari, Pune"),
    ],
    "items": [
        # raw materials
        ("Aluminium Billet 6063", "raw_material", "kg", RM_METAL, 12000),
        ("Aluminium Ingot A356", "raw_material", "kg", RM_METAL, 8000),
        ("Spring Steel Wire EN10270 2.0mm", "raw_material", "kg", RM_METAL, 3500),
        ("Stainless Wire Rod 304 5.5mm", "raw_material", "kg", RM_METAL, 2200),
        ("Cold Rolled Steel Sheet 1.5mm", "raw_material", "sheet", RM_METAL, 640),
        ("Mild Steel Round Bar 12mm", "raw_material", "kg", RM_METAL, 4100),
        ("Zinc Phosphate Coating Powder", "raw_material", "kg", RM_METAL, 300),
        ("Powder Coat Epoxy RAL7035", "raw_material", "kg", RM_METAL, 450),
        ("Hydraulic Oil ISO VG46", "raw_material", "litre", RM_METAL, 800),
        ("Industrial Packing Crate", "raw_material", "unit", RM_METAL, 250),
        # finished goods
        ("Compression Spring HD-40", "finished_good", "unit", FG_METAL, 5200, 22),
        ("Extension Spring ES-25 Zinc", "finished_good", "unit", FG_METAL, 3800, 16),
        ("Torsion Spring TS-12", "finished_good", "unit", FG_METAL, 2600, 14),
        ("Aluminium T-Slot Profile 40x40", "finished_good", "metre", FG_METAL, 1500, 340),
        ("Aluminium Window Frame AW-60", "finished_good", "metre", FG_METAL, 900, 520),
        ("Galvanized Wire Mesh Panel 2x1m", "finished_good", "unit", FG_METAL, 420, 1450),
        ("CNC Gear Blank 80mm", "finished_good", "unit", FG_METAL, 650, 950),
        ("Conveyor Roller Assembly 600mm", "finished_good", "unit", FG_METAL, 310, 2800),
        ("Wire Formed Shelf Bracket", "finished_good", "unit", FG_METAL, 1800, 85),
        ("Industrial Fastener Kit M8", "finished_good", "kit", FG_METAL, 950, 420),
    ],
    "vendors": [
        {"name": "Hindalco Industries Ltd", "category": "raw_material", "email": "sales@hindalco.demo", "phone": "+91-22-66917000", "address": "Mumbai, Maharashtra", "rating": 4.7},
        {"name": "Tata Steel Wiron", "category": "raw_material", "email": "wiron@tatasteel.demo", "phone": "+91-657-2345678", "address": "Jamshedpur, Jharkhand", "rating": 4.8},
        {"name": "JSW Coated Products", "category": "raw_material", "email": "orders@jsw.demo", "phone": "+91-22-42861000", "address": "Vasind, Maharashtra", "rating": 4.3},
        {"name": "Asian Coatings & Chemicals", "category": "raw_material", "email": "b2b@asiancoat.demo", "phone": "+91-20-27475800", "address": "Pimpri, Pune", "rating": 4.1},
    ],
    "vendor_prices": [
        ("Hindalco Industries Ltd", "Aluminium Billet 6063", 262.50, 1000, 10),
        ("Hindalco Industries Ltd", "Aluminium Ingot A356", 248.00, 1000, 10),
        ("Tata Steel Wiron", "Spring Steel Wire EN10270 2.0mm", 96.00, 500, 7),
        ("Tata Steel Wiron", "Stainless Wire Rod 304 5.5mm", 218.00, 250, 12),
        ("JSW Coated Products", "Cold Rolled Steel Sheet 1.5mm", 3450.00, 50, 14),
        ("JSW Coated Products", "Mild Steel Round Bar 12mm", 62.00, 1000, 9),
        ("Asian Coatings & Chemicals", "Zinc Phosphate Coating Powder", 410.00, 50, 5),
        ("Asian Coatings & Chemicals", "Powder Coat Epoxy RAL7035", 385.00, 50, 5),
    ],
    "customers": [
        {"name": "Godrej Appliances", "email": "purchase@godrejapp.demo", "phone": "+91-22-67961700", "address": "Vikhroli, Mumbai"},
        {"name": "L&T Construction Equipment", "email": "vendors@lntce.demo", "phone": "+91-80-25020100", "address": "Bengaluru, Karnataka"},
        {"name": "Blue Star Ltd", "email": "scm@bluestar.demo", "phone": "+91-22-66684000", "address": "Thane, Maharashtra"},
        {"name": "Bajaj Auto Ancillaries", "email": "sourcing@bajajanc.demo", "phone": "+91-20-27472851", "address": "Akurdi, Pune"},
    ],
    "lines": [
        ("Spring Coiling Line 1", "Plant 1 - Bay A", 450),
        ("Wire Drawing Line", "Plant 1 - Bay A", 300),
        ("Extrusion Press 1800T", "Plant 1 - Bay B", 120),
        ("CNC Machining Cell", "Plant 1 - Bay C", 60),
    ],
    "recipes": [
        ("Compression Spring HD-40", [("Spring Steel Wire EN10270 2.0mm", 0.12), ("Zinc Phosphate Coating Powder", 0.005)]),
        ("Extension Spring ES-25 Zinc", [("Spring Steel Wire EN10270 2.0mm", 0.08), ("Zinc Phosphate Coating Powder", 0.004)]),
        ("Aluminium T-Slot Profile 40x40", [("Aluminium Billet 6063", 1.15)]),
        ("Aluminium Window Frame AW-60", [("Aluminium Billet 6063", 1.60), ("Powder Coat Epoxy RAL7035", 0.06)]),
        ("Galvanized Wire Mesh Panel 2x1m", [("Stainless Wire Rod 304 5.5mm", 4.5)]),
        ("Conveyor Roller Assembly 600mm", [("Mild Steel Round Bar 12mm", 2.8), ("Cold Rolled Steel Sheet 1.5mm", 0.5), ("Hydraulic Oil ISO VG46", 0.05)]),
    ],
    "purchase_orders": [
        ("Hindalco Industries Ltd", "approved", [("Aluminium Billet 6063", 5000, 262.50)]),
        ("Tata Steel Wiron", "ordered", [("Spring Steel Wire EN10270 2.0mm", 2000, 96.00), ("Stainless Wire Rod 304 5.5mm", 500, 218.00)]),
        ("Asian Coatings & Chemicals", "pending", [("Powder Coat Epoxy RAL7035", 100, 385.00)]),
    ],
    "sales_orders": [
        ("Godrej Appliances", "confirmed", [("Compression Spring HD-40", 2000), ("Extension Spring ES-25 Zinc", 1500)]),
        ("L&T Construction Equipment", "pending", [("Conveyor Roller Assembly 600mm", 150), ("Industrial Fastener Kit M8", 300)]),
        ("Blue Star Ltd", "shipped", [("Aluminium T-Slot Profile 40x40", 600)]),
    ],
    "production_orders": [
        # (finished good, qty, target warehouse, line, status)
        ("Compression Spring HD-40", 3000, FG_METAL, "Spring Coiling Line 1", "running"),
        ("Extension Spring ES-25 Zinc", 2000, FG_METAL, "Spring Coiling Line 1", "scheduled"),
        ("Aluminium T-Slot Profile 40x40", 800, FG_METAL, "Extrusion Press 1800T", "running"),
        ("Galvanized Wire Mesh Panel 2x1m", 250, FG_METAL, "Wire Drawing Line", "scheduled"),
        ("Conveyor Roller Assembly 600mm", 120, FG_METAL, "CNC Machining Cell", "completed"),
    ],
    # --- logistics ---
    "drivers": [
        {"name": "Suresh Pawar", "license_number": "MH12-HMV-88214", "license_type": "HMV", "phone": "+91-98220-11223", "experience_years": 12},
        {"name": "Vikram Jadhav", "license_number": "MH14-TRV-55901", "license_type": "TRANSPORT", "phone": "+91-98500-44556", "experience_years": 8},
    ],
    "vehicles": [
        {"name": "Tata LPT 1613 Flatbed", "vehicle_type": "Flatbed Truck", "status": "in-use", "driver": "Suresh Pawar", "current_location": "Mumbai-Pune Expressway", "fuel_level": 62, "next_maintenance": "2026-08-15", "capacity": 9000, "current_load": 6400},
        {"name": "Ashok Leyland Ecomet 1215", "vehicle_type": "Container Truck", "status": "available", "driver": "", "current_location": "Dispatch Yard - Plant 1", "fuel_level": 90, "next_maintenance": "2026-09-02", "capacity": 7500, "current_load": 0},
        {"name": "Eicher Pro 2049 LCV", "vehicle_type": "Light Commercial", "status": "maintenance", "driver": "", "current_location": "Plant 1 Workshop", "fuel_level": 35, "next_maintenance": "2026-07-10", "capacity": 3500, "current_load": 0},
    ],
    "routes": [
        # (name, stops, distance, est_time, vehicle, status, efficiency)
        ("Pune-Mumbai OEM Corridor", 4, "165 km", "4.5 hrs", "Tata LPT 1613 Flatbed", "active", 92),
        ("Pune-Bengaluru Machinery Route", 6, "840 km", "14 hrs", "Ashok Leyland Ecomet 1215", "planned", 85),
    ],
    "shipments": [
        # (customer, destination, status, priority, driver, vehicle, route, progress)
        ("Godrej Appliances", "Vikhroli, Mumbai", "in-transit", "high", "Suresh Pawar", "Tata LPT 1613 Flatbed", "Pune-Mumbai OEM Corridor", 65),
        ("Blue Star Ltd", "Thane, Maharashtra", "delivered", "normal", "Vikram Jadhav", "Ashok Leyland Ecomet 1215", "Pune-Mumbai OEM Corridor", 100),
        ("L&T Construction Equipment", "Bengaluru, Karnataka", "preparing", "normal", None, None, "Pune-Bengaluru Machinery Route", 0),
    ],
    # --- workforce ---
    "departments": [("Production", "PRD"), ("Quality", "QLT"), ("Logistics", "LOG"), ("Finance", "FIN"), ("HR", "HR")],
    "employees": [
        {"first_name": "Anil", "last_name": "Kumar", "email": "anil.kumar@apexforge.demo", "phone": "+91-98230-10001", "dept": "Production", "role": "Spring Line Supervisor", "shift": "morning", "assigned_line": "Spring Coiling Line 1", "performance": 94, "attendance": 97, "safety_score": 98},
        {"first_name": "Sanjay", "last_name": "Patil", "email": "sanjay.patil@apexforge.demo", "phone": "+91-98230-10002", "dept": "Production", "role": "Extrusion Press Operator", "shift": "morning", "assigned_line": "Extrusion Press 1800T", "performance": 88, "attendance": 92, "safety_score": 95},
        {"first_name": "Deepak", "last_name": "Verma", "email": "deepak.verma@apexforge.demo", "phone": "+91-98230-10003", "dept": "Production", "role": "CNC Machinist", "shift": "afternoon", "assigned_line": "CNC Machining Cell", "performance": 91, "attendance": 95, "safety_score": 100},
        {"first_name": "Kavita", "last_name": "Joshi", "email": "kavita.joshi@apexforge.demo", "phone": "+91-98230-10004", "dept": "Quality", "role": "Quality Inspector - Metallurgy", "shift": "morning", "performance": 96, "attendance": 99, "safety_score": 100},
        {"first_name": "Ramesh", "last_name": "Gupta", "email": "ramesh.gupta@apexforge.demo", "phone": "+91-98230-10005", "dept": "Logistics", "role": "Dispatch Coordinator", "shift": "morning", "performance": 87, "attendance": 90, "safety_score": 93},
        {"first_name": "Priya", "last_name": "Nair", "email": "priya.nair@apexforge.demo", "phone": "+91-98230-10006", "dept": "HR", "role": "HR Executive", "shift": "morning", "performance": 92, "attendance": 98, "safety_score": 100},
    ],
    "shifts": [
        ("Morning Shift A", "morning", "06:00:00", "14:00:00", 25),
        ("Evening Shift B", "evening", "14:00:00", "22:00:00", 20),
    ],
    # --- finance ---
    "budgets": [
        # (department, period, label, total, auto_approve_limit)
        ("production", "monthly", "Jul 2026", 2500000, 50000),
        ("procurement", "monthly", "Jul 2026", 1800000, 40000),
        ("maintenance", "monthly", "Jul 2026", 400000, 15000),
        ("logistics", "monthly", "Jul 2026", 350000, 10000),
        ("quality", "monthly", "Jul 2026", 250000, 8000),
        ("production", "quarterly", "Q3 2026", 7800000, 50000),
        ("procurement", "quarterly", "Q3 2026", 5600000, 40000),
    ],
    "expenses": [
        # (title, category, amount, vendor, final_status)
        ("Spring steel wire restock - Tata Wiron", "raw_material", 480000, "Tata Steel Wiron", "pending"),
        ("Extrusion die refurbishment", "maintenance", 120000, "Pune Die Works", "approved"),
        ("Forklift annual service contract", "equipment", 65000, "Godrej Material Handling", "approved"),
        ("CNC coolant and cutting fluid stock", "raw_material", 38000, "Castrol Industrial", "approved"),
        ("Plant CCTV upgrade - Bay B", "safety", 145000, "Honeywell Automation", "rejected"),
        ("ISO 9001 surveillance audit fee", "miscellaneous", 55000, "Bureau Veritas", "pending"),
    ],
    "operational_costs": [
        # (title, cost_type, department, amount, date, vendor)
        ("Electricity - induction furnaces Apr 2026", "variable", "production", 352000, "2026-04-28", "MSEDCL"),
        ("Electricity - induction furnaces May 2026", "variable", "production", 365000, "2026-05-28", "MSEDCL"),
        ("Electricity - induction furnaces Jun 2026", "variable", "production", 380000, "2026-06-28", "MSEDCL"),
        ("Factory lease - Plant 1 Jun 2026", "fixed", "admin", 250000, "2026-06-01", "MIDC Bhosari"),
        ("Factory lease - Plant 1 Jul 2026", "fixed", "admin", 250000, "2026-07-01", "MIDC Bhosari"),
        ("Diesel - dispatch fleet Jun 2026", "variable", "logistics", 85000, "2026-06-30", "HP Fuel Station"),
        ("Press hydraulics overhaul", "one_time", "maintenance", 190000, "2026-05-14", "Bosch Rexroth Service"),
    ],
    # --- AI demo scenarios ---
    "equipment": [
        # (name, line, status, health, uptime, last_maint, next_maint)
        ("Coiling Machine CM-201", "Spring Coiling Line 1", "running", 91, 98.2, "2026-06-10", "2026-09-10"),
        ("Wire Draw Bench WD-3", "Wire Drawing Line", "running", 84, 96.5, "2026-05-22", "2026-08-22"),
        ("Extrusion Press EP-1800", "Extrusion Press 1800T", "running", 37, 88.4, "2026-03-02", "2026-07-19"),
        ("CNC VMC Haas VF-2", "CNC Machining Cell", "idle", 76, 94.1, "2026-06-01", "2026-09-01"),
    ],
    "maintenance_tasks": [
        # (equipment, type, status, priority, scheduled, duration, description, technician, amount)
        ("Extrusion Press EP-1800", "predictive", "scheduled", "high", "2026-07-19T09:00:00Z", "8 hours",
         "Hydraulic seal degradation detected - pressure drop trend indicates failure risk within ~12 days. Health at 37%.",
         "Bosch Rexroth Service", 45000),
        ("Coiling Machine CM-201", "preventive", "completed", "medium", "2026-06-10T08:00:00Z", "3 hours",
         "Quarterly lubrication and coil tension calibration.", "In-house - Anil Kumar", 8000),
        ("Wire Draw Bench WD-3", "corrective", "requested", "medium", None, "4 hours",
         "Die block vibration above threshold during 5.5mm rod runs.", None, 15000),
    ],
    "quality_checks": [
        # (recipe/product, status, test_type, parameter, result, target, remarks)
        ("Compression Spring HD-40", "approved", "Load Test", "Spring rate k", "39.6 N/mm", "40 +/-1 N/mm", "Within tolerance."),
        ("Compression Spring HD-40", "approved", "Dimensional", "Free length", "80.2 mm", "80 +/-0.5 mm", ""),
        ("Aluminium T-Slot Profile 40x40", "rejected", "Dimensional", "Slot width", "8.42 mm", "8.2 +/-0.1 mm", "Die wear on EP-1800 suspected - slot oversize."),
        ("Aluminium T-Slot Profile 40x40", "rejected", "Surface", "Anodizing thickness", "8 um", "12-18 um", "Uneven coating, linked to press temperature drift."),
        ("Aluminium T-Slot Profile 40x40", "approved", "Dimensional", "Profile height", "40.05 mm", "40 +/-0.15 mm", ""),
        ("Galvanized Wire Mesh Panel 2x1m", "approved", "Coating", "Zinc coating mass", "278 g/m2", ">= 275 g/m2", ""),
        ("Conveyor Roller Assembly 600mm", "rejected", "Runout", "Radial runout", "0.35 mm", "<= 0.2 mm", "Bearing seat machining rework required."),
        ("Extension Spring ES-25 Zinc", "approved", "Load Test", "Initial tension", "12.1 N", "12 +/-0.5 N", ""),
    ],
    "low_stock": [
        # (item, warehouse, new absolute qty) - triggers AI Procurement flow
        ("Zinc Phosphate Coating Powder", RM_METAL, 12),
        ("Spring Steel Wire EN10270 2.0mm", RM_METAL, 150),
    ],
    "stale_orders": [
        # Backdated later at DB level -> 'customers with no orders in 90 days'
        ("Bajaj Auto Ancillaries", "delivered", [("Wire Formed Shelf Bracket", 400)]),
    ],
    "payroll": [
        # (employee email, period, basic, allowances, tax)
        ("anil.kumar@apexforge.demo", "Jun 2026", 45000, 8000, 4200),
        ("sanjay.patil@apexforge.demo", "Jun 2026", 38000, 6000, 3100),
        ("deepak.verma@apexforge.demo", "Jun 2026", 42000, 7000, 3800),
        ("kavita.joshi@apexforge.demo", "Jun 2026", 40000, 6500, 3500),
        ("ramesh.gupta@apexforge.demo", "Jun 2026", 32000, 5000, 2400),
        ("priya.nair@apexforge.demo", "Jun 2026", 35000, 5500, 2800),
    ],
}

# =====================================================================
# Company 2: PureSweet Naturals
# Plant-based & zero-calorie sweeteners: agave, coconut sugar, dates,
# erythritol, monk fruit, xylitol, stevia; keto/paleo condiments.
# =====================================================================
RM_FOOD = "Ingredient Cold Store - Nashik"
FG_FOOD = "Finished Goods DC - Nashik"

PURESWEET = {
    "company": "PureSweet Naturals",
    "slug": "puresweetnaturals",
    "admin_first": "Paresh",
    "admin_email": "admin@puresweet.com",
    "warehouses": [
        (RM_FOOD, "Sinnar MIDC, Nashik"),
        (FG_FOOD, "Sinnar MIDC, Nashik"),
        ("QC Hold Area", "Sinnar MIDC, Nashik"),
    ],
    "items": [
        # raw materials
        ("Organic Blue Agave Concentrate", "raw_material", "litre", RM_FOOD, 4500),
        ("Coconut Palm Sap Crystals", "raw_material", "kg", RM_FOOD, 3200),
        ("Medjool Date Paste", "raw_material", "kg", RM_FOOD, 1800),
        ("Erythritol Crystals Non-GMO", "raw_material", "kg", RM_FOOD, 6000),
        ("Monk Fruit Extract Mogroside-V 50", "raw_material", "kg", RM_FOOD, 120),
        ("Birch Xylitol Crystals", "raw_material", "kg", RM_FOOD, 2400),
        ("Stevia Reb-A 98 Extract", "raw_material", "kg", RM_FOOD, 85),
        ("Organic Tomato Paste 36 Brix", "raw_material", "kg", RM_FOOD, 1500),
        ("Raw Apple Cider Vinegar", "raw_material", "litre", RM_FOOD, 900),
        ("Amber Glass Bottle 250ml", "raw_material", "unit", RM_FOOD, 20000),
        ("Food Grade PET Jar 500g", "raw_material", "unit", RM_FOOD, 15000),
        ("Kraft Stand-Up Pouch 1kg", "raw_material", "unit", RM_FOOD, 18000),
        # finished goods
        ("Organic Agave Nectar 250ml", "finished_good", "bottle", FG_FOOD, 6200, 240),
        ("Coconut Sugar Pouch 500g", "finished_good", "unit", FG_FOOD, 4800, 180),
        ("Date Syrup Squeeze 340g", "finished_good", "unit", FG_FOOD, 3100, 210),
        ("Erythritol Baking Sweetener 1kg", "finished_good", "unit", FG_FOOD, 5400, 520),
        ("Monk Fruit Sweetener Blend 200g", "finished_good", "unit", FG_FOOD, 7200, 350),
        ("Birch Xylitol Jar 500g", "finished_good", "unit", FG_FOOD, 2900, 410),
        ("Keto Pancake Syrup 250ml", "finished_good", "unit", FG_FOOD, 3600, 380),
        ("Zero-Calorie Ketchup 320g", "finished_good", "unit", FG_FOOD, 2400, 190),
        ("Stevia Liquid Drops 60ml", "finished_good", "unit", FG_FOOD, 4100, 260),
        ("Golden Plant-Based Sweetener 400g", "finished_good", "unit", FG_FOOD, 3300, 300),
    ],
    "vendors": [
        {"name": "AgaveMex Exports SA", "category": "raw_material", "email": "export@agavemex.demo", "phone": "+52-33-36150000", "address": "Guadalajara, Mexico", "rating": 4.6},
        {"name": "Ceylon Coconut Collective", "category": "raw_material", "email": "trade@ceyloncoco.demo", "phone": "+94-11-2695300", "address": "Colombo, Sri Lanka", "rating": 4.4},
        {"name": "Layn Natural Ingredients", "category": "raw_material", "email": "sales@layn.demo", "phone": "+86-773-5820588", "address": "Guilin, China", "rating": 4.7},
        {"name": "Jungbunzlauer Suisse AG", "category": "raw_material", "email": "orders@jbl.demo", "phone": "+41-61-2955100", "address": "Basel, Switzerland", "rating": 4.9},
    ],
    "vendor_prices": [
        ("AgaveMex Exports SA", "Organic Blue Agave Concentrate", 3.80, 1000, 30),
        ("Ceylon Coconut Collective", "Coconut Palm Sap Crystals", 2.90, 500, 21),
        ("Layn Natural Ingredients", "Monk Fruit Extract Mogroside-V 50", 68.00, 25, 25),
        ("Layn Natural Ingredients", "Stevia Reb-A 98 Extract", 55.00, 25, 25),
        ("Jungbunzlauer Suisse AG", "Erythritol Crystals Non-GMO", 2.15, 1000, 18),
        ("Jungbunzlauer Suisse AG", "Birch Xylitol Crystals", 4.60, 500, 18),
    ],
    "customers": [
        {"name": "Whole Foods Market", "email": "vendors@wfm.demo", "phone": "+1-512-4774455", "address": "Austin, TX, USA"},
        {"name": "Thrive Market", "email": "merch@thrive.demo", "phone": "+1-866-4195965", "address": "Los Angeles, CA, USA"},
        {"name": "Sprouts Farmers Market", "email": "buying@sprouts.demo", "phone": "+1-480-8148016", "address": "Phoenix, AZ, USA"},
        {"name": "iHerb LLC", "email": "sourcing@iherb.demo", "phone": "+1-951-6163600", "address": "Irvine, CA, USA"},
    ],
    "lines": [
        ("Syrup Blending & Bottling Line", "Nashik Plant - Hall 1", 1200),
        ("Crystal Sweetener Packing Line", "Nashik Plant - Hall 2", 900),
        ("Sauce Processing Line", "Nashik Plant - Hall 3", 600),
    ],
    "recipes": [
        ("Organic Agave Nectar 250ml", [("Organic Blue Agave Concentrate", 0.26), ("Amber Glass Bottle 250ml", 1)]),
        ("Coconut Sugar Pouch 500g", [("Coconut Palm Sap Crystals", 0.5), ("Kraft Stand-Up Pouch 1kg", 1)]),
        ("Monk Fruit Sweetener Blend 200g", [("Erythritol Crystals Non-GMO", 0.196), ("Monk Fruit Extract Mogroside-V 50", 0.004), ("Kraft Stand-Up Pouch 1kg", 1)]),
        ("Erythritol Baking Sweetener 1kg", [("Erythritol Crystals Non-GMO", 1.0), ("Kraft Stand-Up Pouch 1kg", 1)]),
        ("Zero-Calorie Ketchup 320g", [("Organic Tomato Paste 36 Brix", 0.25), ("Monk Fruit Extract Mogroside-V 50", 0.002), ("Raw Apple Cider Vinegar", 0.03), ("Food Grade PET Jar 500g", 1)]),
        ("Keto Pancake Syrup 250ml", [("Monk Fruit Extract Mogroside-V 50", 0.003), ("Erythritol Crystals Non-GMO", 0.08), ("Amber Glass Bottle 250ml", 1)]),
        ("Date Syrup Squeeze 340g", [("Medjool Date Paste", 0.36), ("Food Grade PET Jar 500g", 1)]),
    ],
    "purchase_orders": [
        ("AgaveMex Exports SA", "ordered", [("Organic Blue Agave Concentrate", 3000, 3.80)]),
        ("Layn Natural Ingredients", "approved", [("Monk Fruit Extract Mogroside-V 50", 50, 68.00), ("Stevia Reb-A 98 Extract", 25, 55.00)]),
        ("Jungbunzlauer Suisse AG", "pending", [("Erythritol Crystals Non-GMO", 4000, 2.15)]),
    ],
    "sales_orders": [
        ("Whole Foods Market", "confirmed", [("Monk Fruit Sweetener Blend 200g", 2400), ("Organic Agave Nectar 250ml", 1800)]),
        ("Thrive Market", "shipped", [("Erythritol Baking Sweetener 1kg", 1200), ("Keto Pancake Syrup 250ml", 900)]),
        ("iHerb LLC", "pending", [("Stevia Liquid Drops 60ml", 1500), ("Zero-Calorie Ketchup 320g", 800)]),
    ],
    "production_orders": [
        ("Organic Agave Nectar 250ml", 5000, FG_FOOD, "Syrup Blending & Bottling Line", "running"),
        ("Monk Fruit Sweetener Blend 200g", 6000, FG_FOOD, "Crystal Sweetener Packing Line", "running"),
        ("Erythritol Baking Sweetener 1kg", 3000, FG_FOOD, "Crystal Sweetener Packing Line", "scheduled"),
        ("Zero-Calorie Ketchup 320g", 2000, FG_FOOD, "Sauce Processing Line", "scheduled"),
        ("Date Syrup Squeeze 340g", 1500, FG_FOOD, "Syrup Blending & Bottling Line", "completed"),
    ],
    "batches": [
        ("Organic Agave Nectar 250ml", "AGV-2026-118", "2028-01-15", 3000),
        ("Monk Fruit Sweetener Blend 200g", "MFB-2026-097", "2028-06-30", 3600),
        ("Zero-Calorie Ketchup 320g", "KET-2026-042", "2027-03-20", 1200),
        ("Date Syrup Squeeze 340g", "DTS-2026-071", "2027-09-10", 1500),
    ],
    # --- logistics ---
    "drivers": [
        {"name": "Ganesh More", "license_number": "MH15-HMV-77120", "license_type": "HMV", "phone": "+91-99700-22334", "experience_years": 10},
        {"name": "Rafiq Shaikh", "license_number": "MH15-TRV-33482", "license_type": "TRANSPORT", "phone": "+91-99230-55667", "experience_years": 6},
    ],
    "vehicles": [
        {"name": "Tata 407 Reefer Van", "vehicle_type": "Refrigerated", "status": "in-use", "driver": "Ganesh More", "current_location": "NH-160 Nashik-Mumbai", "fuel_level": 55, "next_maintenance": "2026-08-20", "capacity": 2500, "current_load": 2100},
        {"name": "BharatBenz 1217C Container", "vehicle_type": "Container Truck", "status": "available", "driver": "", "current_location": "Finished Goods DC - Nashik", "fuel_level": 85, "next_maintenance": "2026-09-12", "capacity": 6000, "current_load": 0},
        {"name": "Mahindra Furio 7 Reefer", "vehicle_type": "Refrigerated", "status": "available", "driver": "", "current_location": "Finished Goods DC - Nashik", "fuel_level": 78, "next_maintenance": "2026-08-05", "capacity": 3500, "current_load": 0},
    ],
    "routes": [
        ("Nashik-JNPT Export Route", 3, "200 km", "5 hrs", "Tata 407 Reefer Van", "active", 94),
        ("Nashik-Mumbai Retail Distribution", 7, "175 km", "6 hrs", "Mahindra Furio 7 Reefer", "planned", 88),
    ],
    "shipments": [
        ("Whole Foods Market", "JNPT Export Terminal, Navi Mumbai", "loading", "urgent", "Ganesh More", "Tata 407 Reefer Van", "Nashik-JNPT Export Route", 20),
        ("Thrive Market", "JNPT Export Terminal, Navi Mumbai", "in-transit", "high", "Rafiq Shaikh", "BharatBenz 1217C Container", "Nashik-JNPT Export Route", 55),
        ("iHerb LLC", "Mumbai Air Cargo Complex", "preparing", "normal", None, None, "Nashik-Mumbai Retail Distribution", 0),
    ],
    # --- workforce ---
    "departments": [("Production", "PRD"), ("Quality", "QLT"), ("Logistics", "LOG"), ("Finance", "FIN"), ("HR", "HR")],
    "employees": [
        {"first_name": "Sunita", "last_name": "Deshmukh", "email": "sunita.deshmukh@puresweet.demo", "phone": "+91-97650-20001", "dept": "Production", "role": "Blending Line Lead", "shift": "morning", "assigned_line": "Syrup Blending & Bottling Line", "performance": 95, "attendance": 98, "safety_score": 100},
        {"first_name": "Imran", "last_name": "Khan", "email": "imran.khan@puresweet.demo", "phone": "+91-97650-20002", "dept": "Production", "role": "Bottling Operator", "shift": "morning", "assigned_line": "Syrup Blending & Bottling Line", "performance": 89, "attendance": 93, "safety_score": 97},
        {"first_name": "Arjun", "last_name": "Reddy", "email": "arjun.reddy@puresweet.demo", "phone": "+91-97650-20003", "dept": "Production", "role": "Packing Line Operator", "shift": "afternoon", "assigned_line": "Crystal Sweetener Packing Line", "performance": 90, "attendance": 94, "safety_score": 96},
        {"first_name": "Meena", "last_name": "Kulkarni", "email": "meena.kulkarni@puresweet.demo", "phone": "+91-97650-20004", "dept": "Quality", "role": "Food Technologist - QC", "shift": "morning", "performance": 97, "attendance": 99, "safety_score": 100},
        {"first_name": "Vishal", "last_name": "Sawant", "email": "vishal.sawant@puresweet.demo", "phone": "+91-97650-20005", "dept": "Logistics", "role": "Cold Chain Coordinator", "shift": "morning", "performance": 88, "attendance": 91, "safety_score": 95},
        {"first_name": "Neha", "last_name": "Agarwal", "email": "neha.agarwal@puresweet.demo", "phone": "+91-97650-20006", "dept": "Finance", "role": "Accounts Executive", "shift": "morning", "performance": 93, "attendance": 97, "safety_score": 100},
    ],
    "shifts": [
        ("Morning Shift A", "morning", "06:00:00", "14:00:00", 25),
        ("Evening Shift B", "evening", "14:00:00", "22:00:00", 20),
    ],
    # --- finance ---
    "budgets": [
        ("production", "monthly", "Jul 2026", 1600000, 30000),
        ("procurement", "monthly", "Jul 2026", 2200000, 60000),
        ("quality", "monthly", "Jul 2026", 300000, 10000),
        ("logistics", "monthly", "Jul 2026", 500000, 15000),
        ("hr", "monthly", "Jul 2026", 200000, 5000),
        ("procurement", "quarterly", "Q3 2026", 6900000, 60000),
        ("logistics", "quarterly", "Q3 2026", 1500000, 15000),
    ],
    "expenses": [
        ("Monk fruit extract import consignment", "raw_material", 950000, "Layn Natural Ingredients", "pending"),
        ("Reefer truck AMC renewal", "transport", 60000, "Tata Motors Service", "approved"),
        ("FSSAI food safety training - line staff", "training", 40000, "TUV SUD India", "approved"),
        ("Amber glass bottles - 40k units", "packaging", 320000, "Piramal Glass", "approved"),
        ("Nitrogen flushing unit for packing line", "equipment", 480000, "Atlas Copco", "pending"),
        ("Organic certification renewal - EU", "miscellaneous", 95000, "Ecocert India", "rejected"),
    ],
    "operational_costs": [
        ("Cold storage electricity Apr 2026", "variable", "production", 195000, "2026-04-28", "MSEDCL"),
        ("Cold storage electricity May 2026", "variable", "production", 208000, "2026-05-28", "MSEDCL"),
        ("Cold storage electricity Jun 2026", "variable", "production", 220000, "2026-06-28", "MSEDCL"),
        ("FSSAI audit & certification FY26", "one_time", "quality", 75000, "2026-06-15", "TUV SUD India"),
        ("Export freight to JNPT May 2026", "variable", "logistics", 132000, "2026-05-30", "Maersk Line"),
        ("Export freight to JNPT Jun 2026", "variable", "logistics", 145000, "2026-06-30", "Maersk Line"),
        ("Facility lease - Sinnar MIDC Jul 2026", "fixed", "admin", 180000, "2026-07-01", "MIDC Sinnar"),
    ],
    # --- AI demo scenarios ---
    "equipment": [
        ("Homogenizer HMG-5", "Syrup Blending & Bottling Line", "running", 41, 89.0, "2026-04-18", "2026-07-16"),
        ("Bottling Filler BF-12", "Syrup Blending & Bottling Line", "running", 88, 97.3, "2026-06-05", "2026-09-05"),
        ("Pouch FFS Machine PF-8", "Crystal Sweetener Packing Line", "running", 79, 95.6, "2026-05-30", "2026-08-30"),
        ("Retort Cooker RC-2", "Sauce Processing Line", "idle", 90, 98.0, "2026-06-20", "2026-09-20"),
    ],
    "maintenance_tasks": [
        ("Homogenizer HMG-5", "predictive", "scheduled", "high", "2026-07-16T09:00:00Z", "6 hours",
         "Bearing vibration and motor temperature rising on HMG-5 - predicted failure in ~12 days. Health at 41%.",
         "GEA Service India", 38000),
        ("Retort Cooker RC-2", "preventive", "completed", "low", "2026-06-20T08:00:00Z", "5 hours",
         "Annual pressure vessel inspection and gasket replacement.", "TUV Technician", 12000),
        ("Pouch FFS Machine PF-8", "corrective", "requested", "medium", None, "3 hours",
         "Sealing jaw temperature fluctuation causing intermittent pouch seal defects.", None, 18000),
    ],
    "quality_checks": [
        ("Organic Agave Nectar 250ml", "approved", "Chemical", "Brix", "76.2", "75-77 Brix", ""),
        ("Organic Agave Nectar 250ml", "approved", "Micro", "Yeast & mold", "<10 cfu/g", "< 100 cfu/g", ""),
        ("Monk Fruit Sweetener Blend 200g", "approved", "Chemical", "Mogroside-V content", "0.98%", "0.9-1.1%", ""),
        ("Zero-Calorie Ketchup 320g", "rejected", "Physical", "Viscosity", "2.9 cm/30s", "3.5-5.5 cm/30s (Bostwick)", "Batch too thick - retort RC-2 temperature profile drifted."),
        ("Zero-Calorie Ketchup 320g", "rejected", "Chemical", "Brix", "33.1", "36-38 Brix", "Under-concentrated tomato solids."),
        ("Zero-Calorie Ketchup 320g", "approved", "Micro", "Total plate count", "220 cfu/g", "< 10000 cfu/g", ""),
        ("Erythritol Baking Sweetener 1kg", "approved", "Physical", "Moisture", "0.11%", "< 0.2%", ""),
        ("Date Syrup Squeeze 340g", "rejected", "Physical", "Fill weight", "331 g", "340 +/-3 g", "Filler head 4 under-dosing on PF-8."),
    ],
    "low_stock": [
        ("Monk Fruit Extract Mogroside-V 50", RM_FOOD, 4),
        ("Amber Glass Bottle 250ml", RM_FOOD, 800),
    ],
    "stale_orders": [
        ("Sprouts Farmers Market", "delivered", [("Coconut Sugar Pouch 500g", 600)]),
    ],
    "payroll": [
        ("sunita.deshmukh@puresweet.demo", "Jun 2026", 42000, 7000, 3600),
        ("imran.khan@puresweet.demo", "Jun 2026", 30000, 5000, 2200),
        ("arjun.reddy@puresweet.demo", "Jun 2026", 31000, 5000, 2300),
        ("meena.kulkarni@puresweet.demo", "Jun 2026", 44000, 7500, 3900),
        ("vishal.sawant@puresweet.demo", "Jun 2026", 34000, 5500, 2600),
        ("neha.agarwal@puresweet.demo", "Jun 2026", 36000, 6000, 2900),
    ],
}


if __name__ == "__main__":
    for spec in (APEXFORGE, PURESWEET):
        print(f"\n=== Seeding {spec['company']} ===")
        seed_company(spec)
    print("All done.")
    print(f"Logins (password {PASSWORD}):")
    print("  ApexForge Metals   -> admin@apexforge.com")
    print("  PureSweet Naturals -> admin@puresweet.com")
