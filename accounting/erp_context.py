"""
ERP Context Integration Layer
Introspects and maps all existing operational ERP modules to Accounting.
Ensures zero duplication of models, respecting existing domain boundaries.
"""

from accounts.models import User
from sales.models import Customer, SalesOrder, Invoice
from procurement.models import Vendor, PurchaseOrder, Bill
from inventory.models import Item, Warehouse, Stock
from production.models import ProductionLine, Recipe, ProductionOrder
from workforce.models import Department, Employee
from finance.models import DepartmentBudget, ExpenseRequest


def get_company_erp_context(company):
    """
    Audits and compiles the live operational ERP context for a given company.
    Provides entity counts, readiness status, and domain boundary mappings.
    """
    if not company:
        return {"error": "Company context required"}

    # 1. Tenancy & Users
    users_qs = User.objects.filter(company=company)
    total_users = users_qs.count()
    roles_breakdown = {}
    for r in ["admin", "finance", "sales", "store", "production", "quality", "hr"]:
        roles_breakdown[r] = users_qs.filter(role=r).count()

    # 2. Customers & Sales
    customers_count = Customer.objects.filter(company=company).count()
    sales_orders_count = SalesOrder.objects.filter(customer__company=company).count()
    invoices_count = Invoice.objects.filter(company=company).count()

    # 3. Vendors & Procurement
    vendors_count = Vendor.objects.filter(company=company).count()
    purchase_orders_count = PurchaseOrder.objects.filter(vendor__company=company).count()
    bills_count = Bill.objects.filter(company=company).count()

    # 4. Inventory & Warehouses
    items_qs = Item.objects.filter(company=company)
    items_count = items_qs.count()
    raw_materials_count = items_qs.filter(category="raw_material").count()
    finished_goods_count = items_qs.filter(category="finished_good").count()
    warehouses_count = Warehouse.objects.filter(company=company).count()
    stock_records_count = Stock.objects.filter(warehouse__company=company).count()

    # 5. Manufacturing & Production
    lines_count = ProductionLine.objects.filter(company=company).count()
    recipes_count = Recipe.objects.filter(product__company=company).count()
    production_orders_count = ProductionOrder.objects.filter(recipe__product__company=company).count()

    # 6. Workforce & Human Capital
    departments_count = Department.objects.filter(company=company).count()
    employees_count = Employee.objects.filter(company=company).count()

    # 7. Existing Finance & Budgets
    budgets_count = DepartmentBudget.objects.filter(company=company).count()
    expenses_count = ExpenseRequest.objects.filter(company=company).count()

    # Domain Readiness Checklist
    readiness = {
        "tenancy": {
            "status": "ready" if total_users > 0 else "needs_setup",
            "name": "Company & User Tenancy",
            "summary": f"{total_users} users active across {len([v for v in roles_breakdown.values() if v > 0])} roles",
        },
        "sales_customers": {
            "status": "ready" if customers_count > 0 else "empty",
            "name": "Customers & Sales",
            "summary": f"{customers_count} customers, {sales_orders_count} sales orders, {invoices_count} invoices",
        },
        "procurement_vendors": {
            "status": "ready" if vendors_count > 0 else "empty",
            "name": "Vendors & Procurement",
            "summary": f"{vendors_count} vendors, {purchase_orders_count} POs, {bills_count} bills",
        },
        "inventory_items": {
            "status": "ready" if items_count > 0 else "empty",
            "name": "Inventory & Stock",
            "summary": f"{items_count} items ({raw_materials_count} raw, {finished_goods_count} FG) across {warehouses_count} warehouses",
        },
        "production_lines": {
            "status": "ready" if lines_count > 0 else "empty",
            "name": "Manufacturing Lines",
            "summary": f"{lines_count} lines, {recipes_count} recipes, {production_orders_count} production orders",
        },
        "workforce": {
            "status": "ready" if employees_count > 0 else "empty",
            "name": "Workforce & Labor",
            "summary": f"{employees_count} employees in {departments_count} departments",
        },
        "finance_budgets": {
            "status": "ready" if budgets_count > 0 else "empty",
            "name": "Departmental Budgets",
            "summary": f"{budgets_count} budgets, {expenses_count} expense requests",
        },
    }

    # Integration Ownership Boundaries Contract
    integration_contract = [
        {
            "domain": "Accounts Receivable (AR)",
            "external_module": "sales",
            "source_model": "sales.Invoice / sales.SalesOrder",
            "accounting_ownership": "Double-entry AR & Sales Revenue journals; Customer subledger balance tracking",
            "status": "mapped",
        },
        {
            "domain": "Accounts Payable (AP)",
            "external_module": "procurement",
            "source_model": "procurement.Bill / procurement.PurchaseOrder",
            "accounting_ownership": "Double-entry AP & Material Expense journals; Vendor subledger balance tracking",
            "status": "mapped",
        },
        {
            "domain": "Inventory Valuation & COGS",
            "external_module": "inventory",
            "source_model": "inventory.StockMovement / inventory.Item",
            "accounting_ownership": "Balance sheet inventory assets & periodic Cost of Goods Sold recognition",
            "status": "mapped",
        },
        {
            "domain": "Work-in-Progress (WIP) & Production",
            "external_module": "production",
            "source_model": "production.ProductionOrder / production.Recipe",
            "accounting_ownership": "Raw material consumption to WIP, direct labor, and finished goods capitalization",
            "status": "mapped",
        },
        {
            "domain": "Payroll & Departmental Expenses",
            "external_module": "workforce & finance",
            "source_model": "workforce.PayrollRecord / finance.ExpenseRequest",
            "accounting_ownership": "Payroll liability accruals, salary disbursements, and operational expense postings",
            "status": "mapped",
        },
    ]

    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "slug": company.slug,
        },
        "counts": {
            "users": total_users,
            "customers": customers_count,
            "sales_orders": sales_orders_count,
            "invoices": invoices_count,
            "vendors": vendors_count,
            "purchase_orders": purchase_orders_count,
            "bills": bills_count,
            "items": items_count,
            "raw_materials": raw_materials_count,
            "finished_goods": finished_goods_count,
            "warehouses": warehouses_count,
            "stock_records": stock_records_count,
            "production_lines": lines_count,
            "recipes": recipes_count,
            "production_orders": production_orders_count,
            "departments": departments_count,
            "employees": employees_count,
            "budgets": budgets_count,
            "expenses": expenses_count,
        },
        "roles_breakdown": roles_breakdown,
        "readiness": readiness,
        "integration_contract": integration_contract,
    }
