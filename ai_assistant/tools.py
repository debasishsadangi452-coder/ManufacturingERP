import json
from django.db.models import Sum
from inventory.models import Stock, Item, Warehouse
from sales.models import SalesOrder
from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor, VendorPriceList, GoodsReceipt
from production.models import ProductionOrder, ProductionLine, Recipe, RecipeIngredient
from maintenance.models import Equipment, MaintenanceTask
from quality.models import QualityCheck
from sales.models import SalesOrder, SalesOrderItem, Customer
from finance.models import ExpenseRequest, DepartmentBudget
from workforce.models import Employee, AttendanceRecord, LeaveRequest, LeaveType
from logistics.models import Vehicle, DeliveryRoute

def get_inventory_summary(user, item_name=None, warehouse_id=None):
    """Returns stock levels for all items or a specific item, optionally filtered by warehouse."""
    if user.role not in ['admin', 'store', 'production']:
        return json.dumps({"error": "Unauthorized access to inventory data."})
    
    stocks = Stock.objects.filter(item__company=user.company).select_related('item', 'warehouse')
    if item_name:
        stocks = stocks.filter(item__name__icontains=item_name)
    if warehouse_id:
        stocks = stocks.filter(warehouse_id=warehouse_id)
    
    results = []
    for s in stocks:
        results.append({
            "item": s.item.name,
            "warehouse": s.warehouse.name,
            "quantity": s.quantity,
            "unit": s.item.unit
        })
    
    return json.dumps(results)

def adjust_stock(user, item_id, warehouse_id, quantity, reason="AI Adjustment"):
    """Adjusts stock levels for a specific item in a warehouse."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized to adjust stock."})
    
    try:
        item = Item.objects.get(id=item_id, company=user.company)
        warehouse = Warehouse.objects.get(id=warehouse_id, company=user.company)
        stock, created = Stock.objects.get_or_create(item=item, warehouse=warehouse)
        stock.quantity += quantity
        stock.save()
        return json.dumps({"success": True, "new_quantity": stock.quantity})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_pending_sales_orders(user):
    """Lists all sales orders that are currently pending."""
    if user.role not in ['admin', 'sales']:
        return json.dumps({"error": "Unauthorized access to sales data."})
    
    orders = SalesOrder.objects.filter(status='pending', customer__company=user.company)
    results = [{"id": o.id, "customer": o.customer.name, "total": str(o.total_amount)} for o in orders]
    return json.dumps(results)

def create_purchase_order(user, vendor_id, items):
    """
    Creates a purchase order for a vendor.
    'items' is a list of dicts with 'item_id' and 'quantity'.
    """
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized to create purchase orders."})
    
    try:
        vendor = Vendor.objects.get(id=vendor_id, company=user.company)
        po = PurchaseOrder.objects.create(vendor=vendor, status='draft')
        
        for entry in items:
            item = Item.objects.get(id=entry['item_id'], company=user.company)
            # Try to get unit price from vendor price list
            price_entry = VendorPriceList.objects.filter(vendor=vendor, item=item).first()
            unit_price = price_entry.unit_price if price_entry else 0
            
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                item=item,
                quantity=entry['quantity'],
                unit_price=unit_price
            )
        
        po.recalculate_total()
        return json.dumps({"success": True, "po_id": po.id, "total": str(po.total_amount)})
    except Exception as e:
        return json.dumps({"error": str(e)})

def check_production_status(user):
    """Returns a summary of running production orders."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized access to production data."})
    
    orders = ProductionOrder.objects.filter(status='running', recipe__product__company=user.company)
    results = [{"id": o.id, "product": o.recipe.product.name, "quantity": o.quantity} for o in orders]
    return json.dumps(results)

def get_equipment_health(user):
    """Returns health status of all production equipment."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized access to equipment data."})
    
    equip = Equipment.objects.filter(line__company=user.company)
    results = [{"id": e.id, "name": e.name, "status": e.status, "health": e.health} for e in equip]
    return json.dumps(results)

def schedule_maintenance(user, equipment_id, task_type, priority, description):
    """Schedules a maintenance task for a piece of equipment."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized to schedule maintenance."})
    
    try:
        equip = Equipment.objects.get(id=equipment_id, line__company=user.company)
        task = MaintenanceTask.objects.create(
            equipment=equip,
            task_type=task_type,
            priority=priority,
            description=description,
            status='requested',
            initiated_by=user
        )
        return json.dumps({"success": True, "task_id": task.id})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_vendors(user):
    """Lists all available vendors."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized access to vendor data."})
    
    vendors = Vendor.objects.filter(company=user.company)
    results = [{"id": v.id, "name": v.name} for v in vendors]
    return json.dumps(results)

def get_finance_overview(user):
    """Returns basic financial indicators."""
    if user.role not in ['admin', 'finance']:
        return json.dumps({"error": "Unauthorized access to financial data."})
    
    from sales.models import SalesOrder
    from finance.models import ExpenseRequest
    
    income = SalesOrder.objects.filter(status__in=['confirmed', 'shipped', 'delivered'], customer__company=user.company).aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    expense = ExpenseRequest.objects.filter(status__in=['approved', 'auto_approved'], company=user.company).aggregate(Sum('amount'))['amount__sum'] or 0
    
    return json.dumps({
        "total_revenue": str(income),
        "total_expense": str(expense),
        "net_profit": str(income - expense)
    })

def list_production_recipes(user):
    """Lists all available production recipes."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized access to recipe data."})
    
    try:
        from production.models import Recipe
        recipes = Recipe.objects.filter(product__company=user.company)
        results = [{"id": r.id, "product": r.product.name} for r in recipes]
        return json.dumps(results)
    except ImportError:
        return json.dumps({"error": "Recipe model not available in production app."})

def list_warehouses(user):
    """Lists all available warehouses."""
    if user.role not in ['admin', 'store', 'production']:
        return json.dumps({"error": "Unauthorized access to warehouse data."})
    
    warehouses = Warehouse.objects.filter(company=user.company)
    results = [{"id": w.id, "name": w.name, "location": w.location} for w in warehouses]
    return json.dumps(results)

def list_items(user):
    """Lists all items in the system."""
    items = Item.objects.filter(company=user.company)
    results = [{"id": i.id, "name": i.name, "category": i.category} for i in items]
    return json.dumps(results)

def create_warehouse(user, name, location):
    """Creates a new warehouse."""
    if user.role != 'admin':
        return json.dumps({"error": "Only admins can create warehouses."})
    try:
        warehouse = Warehouse.objects.create(name=name, location=location, company=user.company)
        return json.dumps({"success": True, "warehouse_id": warehouse.id, "name": warehouse.name})
    except Exception as e:
        return json.dumps({"error": str(e)})

def delete_warehouse(user, warehouse_id):
    """Deletes a warehouse and its associated stock if empty."""
    if user.role != 'admin':
        return json.dumps({"error": "Only admins can delete warehouses."})
    try:
        warehouse = Warehouse.objects.get(id=warehouse_id, company=user.company)
        # Check if there is stock
        has_stock = Stock.objects.filter(warehouse=warehouse, quantity__gt=0).exists()
        if has_stock:
            return json.dumps({"error": "Cannot delete warehouse with active stock. Move or adjust stock to 0 first."})
        
        warehouse.delete()
        return json.dumps({"success": True, "message": f"Warehouse {warehouse_id} deleted."})
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_item(user, name, category, unit="unit", warehouse_id=None, initial_quantity=0):
    """Creates a new item and optionally links it to a warehouse with initial stock."""
    if user.role != 'admin':
        return json.dumps({"error": "Only admins can create items."})
    try:
        item = Item.objects.create(name=name, category=category, unit=unit, company=user.company)
        
        if warehouse_id:
            warehouse = Warehouse.objects.get(id=warehouse_id, company=user.company)
            Stock.objects.create(item=item, warehouse=warehouse, quantity=initial_quantity)
            
        return json.dumps({
            "success": True, 
            "item_id": item.id, 
            "name": item.name,
            "warehouse_id": warehouse_id,
            "warehouse_name": warehouse.name if warehouse_id else None,
            "initial_quantity": initial_quantity
        })
    except Exception as e:
        return json.dumps({"error": str(e)})

def delete_item(user, item_id):
    """Deletes an item from the system."""
    if user.role != 'admin':
        return json.dumps({"error": "Only admins can delete items."})
    try:
        item = Item.objects.get(id=item_id, company=user.company)
        item.delete()
        return json.dumps({"success": True, "message": f"Item {item_id} deleted."})
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_vendor(user, name):
    """Creates a new vendor."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized to create vendors."})
    try:
        vendor = Vendor.objects.create(name=name, company=user.company)
        return json.dumps({"success": True, "vendor_id": vendor.id, "name": vendor.name})
    except Exception as e:
        return json.dumps({"error": str(e)})

def delete_vendor(user, vendor_id):
    """Deletes a vendor."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized to delete vendors."})
    try:
        vendor = Vendor.objects.get(id=vendor_id, company=user.company)
        vendor.delete()
        return json.dumps({"success": True, "message": f"Vendor {vendor_id} deleted."})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_production_lines(user):
    """Lists production lines with status/capacity so the user can pick one."""
    lines = ProductionLine.objects.filter(company=user.company)
    return json.dumps([
        {"id": l.id, "name": l.name, "location": l.location, "status": l.status,
         "capacity_per_hour": l.capacity, "is_active": l.is_active}
        for l in lines
    ])

def create_production_order(user, recipe_id, quantity, warehouse_id, line_id=None):
    """Creates a new production order on a specific production line."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized to create production orders."})
    
    try:
        lines_qs = ProductionLine.objects.filter(company=user.company, is_active=True)
        if line_id in (None, "", 0):
            return json.dumps({
                "error": "line_id is required. Show the user this list of production lines and ask which one to run the batch on before creating the order.",
                "available_lines": [
                    {"id": l.id, "name": l.name, "status": l.status} for l in lines_qs
                ],
            })
        if isinstance(line_id, str) and not str(line_id).isdigit():
            line = lines_qs.filter(name__icontains=line_id).first()
            if not line:
                raise ValueError(f"Production line '{line_id}' not found.")
        else:
            line = lines_qs.filter(id=int(line_id)).first()
            if not line:
                raise ValueError(f"Production line #{line_id} not found.")
        if line.status == 'maintenance':
            return json.dumps({
                "error": f"Line '{line.name}' is under maintenance. Ask the user to pick another line.",
                "available_lines": [
                    {"id": l.id, "name": l.name, "status": l.status}
                    for l in lines_qs.exclude(id=line.id)
                ],
            })
        if isinstance(recipe_id, str) and not str(recipe_id).isdigit():
            recipe = Recipe.objects.filter(product__name__icontains=recipe_id, product__company=user.company).first()
            if not recipe:
                raise ValueError(f"Recipe for product '{recipe_id}' not found.")
        else:
            recipe = Recipe.objects.get(id=int(recipe_id), product__company=user.company)

        if isinstance(warehouse_id, str) and not str(warehouse_id).isdigit():
            warehouse = Warehouse.objects.filter(name__icontains=warehouse_id, company=user.company).first()
            if not warehouse:
                raise ValueError(f"Warehouse '{warehouse_id}' not found.")
        else:
            warehouse = Warehouse.objects.get(id=int(warehouse_id), company=user.company)

        order = ProductionOrder.objects.create(
            recipe=recipe,
            quantity=float(quantity),
            warehouse=warehouse,
            line=line,
            status='scheduled'
        )
        return json.dumps({"success": True, "order_id": order.id, "product": recipe.product.name, "quantity": quantity, "line": line.name})
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_work_order(user, recipe_id, quantity, warehouse_id, line_id=None):
    """Alias for create_production_order to handle alternative naming."""
    return create_production_order(user, recipe_id, quantity, warehouse_id, line_id)

def update_production_status(user, order_id, status):
    """Updates the status of a production order."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized to update production orders."})
    try:
        order = ProductionOrder.objects.get(id=order_id, recipe__product__company=user.company)
        order.status = status
        order.save()
        return json.dumps({"success": True, "order_id": order.id, "new_status": status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_production_orders(user):
    """Lists all production orders and their current status."""
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized access to production data."})
    orders = ProductionOrder.objects.filter(recipe__product__company=user.company).order_by('-created_at')
    results = [
        {
            "id": o.id,
            "product": o.recipe.product.name,
            "quantity": o.quantity,
            "status": o.status,
            "warehouse": o.warehouse.name,
            "created_at": o.created_at.strftime('%Y-%m-%d %H:%M')
        }
        for o in orders[:20]  # Limit to 20 for token efficiency
    ]
    return json.dumps(results)

def create_recipe(user, product_id, ingredients):
    """
    Creates a new production recipe.
    'ingredients' is a list of dicts with 'item_id' and 'quantity'.
    """
    if user.role not in ['admin', 'production']:
        return json.dumps({"error": "Unauthorized to create recipes."})
    try:
        product = Item.objects.get(id=product_id, company=user.company)
        recipe = Recipe.objects.create(product=product)
        for ing in ingredients:
            item = Item.objects.get(id=ing['item_id'], company=user.company)
            RecipeIngredient.objects.create(recipe=recipe, item=item, quantity=ing['quantity'])
        return json.dumps({"success": True, "recipe_id": recipe.id, "product": product.name})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_customers(user):
    """Lists all available customers."""
    if user.role not in ['admin', 'sales']:
        return json.dumps({"error": "Unauthorized access to customer data."})
    customers = Customer.objects.filter(company=user.company)
    results = [{"id": c.id, "name": c.name} for c in customers]
    return json.dumps(results)

def create_customer(user, name, email=""):
    """Creates a new customer."""
    if user.role not in ['admin', 'sales']:
        return json.dumps({"error": "Unauthorized to create customers."})
    try:
        customer = Customer.objects.create(name=name, email=email, company=user.company)
        return json.dumps({"success": True, "customer_id": customer.id, "name": customer.name})
    except Exception as e:
        return json.dumps({"error": str(e)})

def delete_customer(user, customer_id):
    """Deletes a customer."""
    if user.role not in ['admin', 'sales']:
        return json.dumps({"error": "Unauthorized to delete customers."})
    try:
        customer = Customer.objects.get(id=customer_id, company=user.company)
        customer.delete()
        return json.dumps({"success": True, "message": f"Customer {customer_id} deleted."})
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_sales_order(user, customer_id, items):
    """
    Creates a new sales order.
    'items' is a list of dicts with 'item_id' and 'quantity'.
    """
    if user.role not in ['admin', 'sales']:
        return json.dumps({"error": "Unauthorized to create sales orders."})
    try:
        customer = Customer.objects.get(id=customer_id, company=user.company)
        so = SalesOrder.objects.create(customer=customer, status='pending')
        for itm in items:
            item = Item.objects.get(id=itm['item_id'], company=user.company)
            SalesOrderItem.objects.create(sales_order=so, item=item, quantity=itm['quantity'])
        return json.dumps({"success": True, "so_id": so.id, "customer": customer.name})
    except Exception as e:
        return json.dumps({"error": str(e)})

def update_purchase_order_status(user, po_id, status):
    """Updates the status of a purchase order."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized to update purchase orders."})
    try:
        po = PurchaseOrder.objects.get(id=po_id, vendor__company=user.company)
        if status == 'received':
            return json.dumps({
                "error": "To receive a PO (and book the stock into inventory), use create_goods_receipt "
                         "with a warehouse. Ask the user which warehouse the goods arrived at.",
            })
        po.status = status
        po.save()
        return json.dumps({"success": True, "po_id": po.id, "new_status": status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_goods_receipt(user, po_id, warehouse_id):
    """Records a goods receipt for a purchase order and books the stock in."""
    if user.role not in ['admin', 'store']:
        return json.dumps({"error": "Unauthorized to record goods receipts."})
    try:
        from inventory.services import increase_stock
        po = PurchaseOrder.objects.get(id=po_id, vendor__company=user.company)
        if po.status not in ('approved', 'ordered'):
            return json.dumps({
                "error": f"PO #{po.id} is '{po.status}'. Only approved or ordered POs can be received."
            })
        if isinstance(warehouse_id, str) and not str(warehouse_id).isdigit():
            warehouse = Warehouse.objects.filter(name__icontains=warehouse_id, company=user.company).first()
            if not warehouse:
                raise ValueError(f"Warehouse '{warehouse_id}' not found.")
        else:
            warehouse = Warehouse.objects.get(id=int(warehouse_id), company=user.company)
        receipt = GoodsReceipt.objects.create(purchase_order=po, warehouse=warehouse)
        received = []
        for poi in po.items.all():
            increase_stock(poi.item, warehouse, poi.quantity, user=user,
                           reference=f"GRN PO#{po.id} (AI)")
            received.append(f"{poi.quantity} x {poi.item.name}")
        po.status = 'received'
        po.save()
        from finance.services import record_procurement_cost
        cost = record_procurement_cost(po, user=user)
        return json.dumps({
            "success": True, "receipt_id": receipt.id, "po_id": po.id,
            "stock_booked_into": warehouse.name, "items_received": received,
            "finance_cost_recorded": float(po.total_amount or 0) if cost else "already recorded",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_expense_request(user, title, amount, category, budget_id=None):
    """Submits a new expense request."""
    try:
        budget = DepartmentBudget.objects.get(id=budget_id, company=user.company) if budget_id else None
        expense = ExpenseRequest.objects.create(company=user.company,
            title=title,
            amount=amount,
            category=category,
            budget=budget,
            requested_by=user,
            status='pending'
        )
        return json.dumps({"success": True, "expense_id": expense.id, "status": expense.status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def approve_expense_request(user, expense_id, notes="AI Approved"):
    """Approves an expense request."""
    if user.role not in ['admin', 'finance']:
        return json.dumps({"error": "Unauthorized to approve expenses."})
    try:
        expense = ExpenseRequest.objects.get(id=expense_id, company=user.company)
        expense.status = 'approved'
        expense.reviewed_by = user
        expense.notes = notes
        expense.save()
        return json.dumps({"success": True, "expense_id": expense.id, "status": expense.status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_quality_checks(user, production_order_id=None):
    """Lists quality control checks, optionally filtered by production order."""
    if user.role not in ['admin', 'quality', 'production']:
        return json.dumps({"error": "Unauthorized access to quality data."})
    checks = QualityCheck.objects.filter(production_order__recipe__product__company=user.company)
    if production_order_id:
        checks = checks.filter(production_order_id=production_order_id)
    results = [
        {
            "id": c.id, 
            "production_order": c.production_order.id, 
            "status": c.status, 
            "test": c.test_type, 
            "result": c.result
        } for c in checks
    ]
    return json.dumps(results)

def record_quality_check(user, production_order_id, test_type, status, result="", remarks=""):
    """Records a new quality control check for a production order."""
    if user.role not in ['admin', 'quality']:
        return json.dumps({"error": "Unauthorized to record quality checks."})
    try:
        order = ProductionOrder.objects.get(id=production_order_id, recipe__product__company=user.company)
        check = QualityCheck.objects.create(
            production_order=order,
            test_type=test_type,
            status=status,
            result=result,
            remarks=remarks
        )
        return json.dumps({"success": True, "check_id": check.id, "status": check.status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_employees(user, department_id=None):
    """Lists employees, optionally filtered by department."""
    employees = Employee.objects.filter(company=user.company).select_related('department', 'job_role')
    if department_id:
        employees = employees.filter(department_id=department_id)
    results = [
        {
            "id": e.id, 
            "employee_id": e.employee_id, 
            "name": e.name, 
            "department": e.department.name if e.department else "N/A",
            "role": e.role,
            "status": e.status
        } for e in employees
    ]
    return json.dumps(results)

def get_employee_details(user, employee_id):
    """Returns detailed information for a specific employee."""
    try:
        e = Employee.objects.get(id=employee_id, company=user.company)
        return json.dumps({
            "id": e.id,
            "employee_id": e.employee_id,
            "name": e.name,
            "email": e.email,
            "phone": e.phone,
            "department": e.department.name if e.department else "N/A",
            "role": e.role,
            "shift": e.shift,
            "status": e.status,
            "date_joined": str(e.date_joined),
            "performance": e.performance,
            "attendance_score": e.attendance
        })
    except Exception as e_err:
        return json.dumps({"error": str(e_err)})

def submit_leave_request(user, employee_id, leave_type_code, start_date, end_date, reason=""):
    """Submits a leave request for an employee."""
    if user.role not in ['admin', 'hr'] and not (hasattr(user, 'employee_profile') and user.employee_profile.id == employee_id):
         return json.dumps({"error": "Unauthorized to submit leave for this employee."})
    try:
        employee = Employee.objects.get(id=employee_id, company=user.company)
        leave_type = LeaveType.objects.get(code=leave_type_code)
        request = LeaveRequest.objects.create(
            employee=employee,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            status='pending'
        )
        return json.dumps({"success": True, "request_id": request.id, "status": request.status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_attendance(user, date=None, employee_id=None):
    """Lists attendance records for a specific date or employee."""
    records = AttendanceRecord.objects.filter(employee__company=user.company).select_related('employee')
    if date:
        records = records.filter(date=date)
    if employee_id:
        records = records.filter(employee_id=employee_id)
    
    results = [
        {
            "employee": r.employee.name,
            "date": str(r.date),
            "status": r.status,
            "hours": str(r.working_hours)
        } for r in records[:50]
    ]
    return json.dumps(results)

def list_vehicles(user):
    """Lists all logistics vehicles."""
    vehicles = Vehicle.objects.filter(company=user.company)
    results = [
        {
            "id": v.id, 
            "name": v.name, 
            "type": v.vehicle_type, 
            "status": v.status, 
            "driver": v.driver,
            "load": f"{v.current_load}/{v.capacity}"
        } for v in vehicles
    ]
    return json.dumps(results)

def update_vehicle_status(user, vehicle_id, status, driver=None):
    """Updates the status and optionally the driver of a vehicle."""
    if user.role not in ['admin', 'logistics', 'store']:
        return json.dumps({"error": "Unauthorized to update vehicle status."})
    try:
        vehicle = Vehicle.objects.get(id=vehicle_id, company=user.company)
        vehicle.status = status
        if driver:
            vehicle.driver = driver
        vehicle.save()
        return json.dumps({"success": True, "vehicle_id": vehicle.id, "new_status": vehicle.status})
    except Exception as e:
        return json.dumps({"error": str(e)})

def list_delivery_routes(user):
    """Lists all planned and active delivery routes."""
    routes = DeliveryRoute.objects.filter(company=user.company).select_related('assigned_vehicle')
    results = [
        {
            "name": r.name,
            "stops": r.stops,
            "status": r.status,
            "vehicle": r.assigned_vehicle.name if r.assigned_vehicle else "Unassigned",
            "efficiency": f"{r.efficiency}%"
        } for r in routes
    ]
    return json.dumps(results)

# Map of tool names to actual functions
TOOL_MAP = {
    "get_inventory_summary": get_inventory_summary,
    "adjust_stock": adjust_stock,
    "list_pending_sales_orders": list_pending_sales_orders,
    "create_purchase_order": create_purchase_order,
    "check_production_status": check_production_status,
    "get_equipment_health": get_equipment_health,
    "schedule_maintenance": schedule_maintenance,
    "list_vendors": list_vendors,
    "get_finance_overview": get_finance_overview,
    "list_production_recipes": list_production_recipes,
    "create_production_order": create_production_order,
    "list_production_lines": list_production_lines,
    "list_warehouses": list_warehouses,
    "list_items": list_items,
    "create_warehouse": create_warehouse,
    "create_item": create_item,
    "create_vendor": create_vendor,
    "update_production_status": update_production_status,
    "create_recipe": create_recipe,
    "list_customers": list_customers,
    "create_customer": create_customer,
    "create_sales_order": create_sales_order,
    "update_purchase_order_status": update_purchase_order_status,
    "create_goods_receipt": create_goods_receipt,
    "create_expense_request": create_expense_request,
    "approve_expense_request": approve_expense_request,
    "list_quality_checks": list_quality_checks,
    "record_quality_check": record_quality_check,
    "list_employees": list_employees,
    "get_employee_details": get_employee_details,
    "submit_leave_request": submit_leave_request,
    "list_attendance": list_attendance,
    "list_vehicles": list_vehicles,
    "update_vehicle_status": update_vehicle_status,
    "list_delivery_routes": list_delivery_routes,
    "delete_warehouse": delete_warehouse,
    "delete_item": delete_item,
    "delete_vendor": delete_vendor,
    "delete_customer": delete_customer,
    "create_work_order": create_work_order,
    "list_production_orders": list_production_orders,
}

# Tool definitions for Groq
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "get_inventory_summary",
            "description": "Get stock levels.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string"},
                    "warehouse_id": {"type": "integer"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_stock",
            "description": "Adjust stock quantity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "warehouse_id": {"type": "integer"},
                    "quantity": {"type": "number"},
                },
                "required": ["item_id", "warehouse_id", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pending_sales_orders",
            "description": "List all sales orders that are pending fulfillment.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_purchase_order",
            "description": "Create a new purchase order for a vendor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "integer"},
                                "quantity": {"type": "number"},
                            },
                            "required": ["item_id", "quantity"],
                        },
                    },
                },
                "required": ["vendor_id", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_production_status",
            "description": "Check the status of currently running production batches.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_equipment_health",
            "description": "Get the health and status of manufacturing equipment.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_maintenance",
            "description": "Schedule a maintenance task for a specific piece of equipment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "equipment_id": {"type": "integer"},
                    "task_type": {"type": "string", "enum": ["preventive", "corrective", "predictive"]},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    "description": {"type": "string"},
                },
                "required": ["equipment_id", "task_type", "priority", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_vendors",
            "description": "List all vendors for procurement.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_finance_overview",
            "description": "Get an overview of the company financial status (income/expenses).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_production_recipes",
            "description": "List all available production recipes (e.g., Cola, Orange Juice).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_production_order",
            "description": "Schedule a new production batch for a product on a chosen production line. IMPORTANT: never pick the line yourself - first call list_production_lines, show the options to the user, and ask which line to use.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string", "description": "ID or name of the product recipe"},
                    "quantity": {"type": "number"},
                    "warehouse_id": {"type": "string", "description": "Target warehouse ID or name for finished goods"},
                    "line_id": {"type": "string", "description": "ID or name of the production line the USER selected from list_production_lines"},
                },
                "required": ["recipe_id", "quantity", "warehouse_id", "line_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_production_lines",
            "description": "List the production lines (name, status, capacity). Call this and show the options whenever the user wants to create a production/work order, so they can choose the line.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_warehouses",
            "description": "List all physical warehouses in the ERP.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_items",
            "description": "List all items (raw materials and finished goods) in the system.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_warehouse",
            "description": "Create a new physical warehouse in the ERP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the warehouse"},
                    "location": {"type": "string", "description": "Physical location/address"},
                },
                "required": ["name", "location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_item",
            "description": "Create new item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string", "enum": ["raw_material", "finished_good"]},
                    "unit": {"type": "string"},
                    "warehouse_id": {"type": "integer"},
                    "initial_quantity": {"type": "number"},
                },
                "required": ["name", "category", "warehouse_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_vendor",
            "description": "Add a new vendor to the procurement system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_production_status",
            "description": "Update the workflow status of a production order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["scheduled", "running", "completed", "delayed"]},
                },
                "required": ["order_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_recipe",
            "description": "Create a manufacturing recipe with ingredients.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "integer"},
                                "quantity": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["product_id", "ingredients"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_customers",
            "description": "List all customers in the sales system.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_customer",
            "description": "Register a new customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_sales_order",
            "description": "Create a new sales order for a customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "integer"},
                                "quantity": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["customer_id", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_purchase_order_status",
            "description": "Approve or update the status of a purchase order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "po_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["draft", "pending", "approved", "ordered", "cancelled"]},
                },
                "required": ["po_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_goods_receipt",
            "description": "Record the arrival of goods for a purchase order into a warehouse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "po_id": {"type": "integer"},
                    "warehouse_id": {"type": "integer"},
                },
                "required": ["po_id", "warehouse_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_expense_request",
            "description": "Submit a request for operational expenditure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number"},
                    "category": {"type": "string"},
                    "budget_id": {"type": "integer"},
                },
                "required": ["title", "amount", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_expense_request",
            "description": "Approve a pending expense request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expense_id": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["expense_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_quality_checks",
            "description": "List quality control inspection results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "production_order_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_quality_check",
            "description": "Log a new quality inspection for a production batch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "production_order_id": {"type": "integer"},
                    "test_type": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "approved", "rejected"]},
                    "result": {"type": "string"},
                    "remarks": {"type": "string"},
                },
                "required": ["production_order_id", "test_type", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_employees",
            "description": "List company employees and their current status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_details",
            "description": "Get detailed profile information for an employee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "integer"},
                },
                "required": ["employee_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_leave_request",
            "description": "Submit a leave of absence request for an employee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "integer"},
                    "leave_type_code": {"type": "string", "description": "e.g., 'AL' for Annual Leave"},
                    "start_date": {"type": "string", "format": "date"},
                    "end_date": {"type": "string", "format": "date"},
                    "reason": {"type": "string"},
                },
                "required": ["employee_id", "leave_type_code", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_attendance",
            "description": "List employee attendance records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "employee_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_vehicles",
            "description": "List all vehicles in the logistics fleet.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_vehicle_status",
            "description": "Update vehicle status or assign a driver.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vehicle_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["available", "in-use", "maintenance"]},
                    "driver": {"type": "string"},
                },
                "required": ["vehicle_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_delivery_routes",
            "description": "List all planned and active delivery routes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_warehouse",
            "description": "Delete a physical warehouse (Admin only). Warehouse must be empty.",
            "parameters": {
                "type": "object",
                "properties": {
                    "warehouse_id": {"type": "integer"},
                },
                "required": ["warehouse_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_item",
            "description": "Delete an item from the system (Admin only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_vendor",
            "description": "Delete a vendor from the system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_id": {"type": "integer"},
                },
                "required": ["vendor_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_customer",
            "description": "Remove a customer from the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_work_order",
            "description": "Create a new production work order or batch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string", "description": "ID or name of the product recipe"},
                    "quantity": {"type": "number"},
                    "warehouse_id": {"type": "string", "description": "Target warehouse ID or name"},
                },
                "required": ["recipe_id", "quantity", "warehouse_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_production_orders",
            "description": "Retrieve a list of all current production orders and batches.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

