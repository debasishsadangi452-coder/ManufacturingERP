"""
Finance Module – Full Backend API Test Suite
Run: python test_finance.py
"""
import os, django, json, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from accounts.models import User
from finance.models import DepartmentBudget, ExpenseRequest, PayrollRecord, OperationalCost
from decimal import Decimal

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
HEAD = "\033[94m"
RESET = "\033[0m"
results = {"pass": 0, "fail": 0}

def ok(name):
    print(f"   {PASS}  {name}")
    results["pass"] += 1

def fail(name, detail=""):
    print(f"   {FAIL}  {name}" + (f" → {detail}" if detail else ""))
    results["fail"] += 1

def section(title):
    print(f"\n{HEAD}{'─'*55}{RESET}")
    print(f"{HEAD} {title}{RESET}")
    print(f"{HEAD}{'─'*55}{RESET}")

def check(cond, name, detail=""):
    if cond:
        ok(name)
    else:
        fail(name, detail)

# ─── Setup clients ─────────────────────────────────────────────────────────
admin = APIClient()
store = APIClient()
quality = APIClient()

def get_token(username, password="password123"):
    from rest_framework_simplejwt.tokens import RefreshToken
    try:
        user = User.objects.get(username=username)
        tokens = RefreshToken.for_user(user)
        return str(tokens.access_token)
    except User.DoesNotExist:
        return None

admin_token = get_token("admin_user")
store_token = get_token("store_user")
quality_token = get_token("quality_user")

if not admin_token:
    print("❌ admin_user not found. Run: python setup_roles.py")
    sys.exit(1)

admin.credentials(HTTP_AUTHORIZATION=f"Bearer {admin_token}")
store.credentials(HTTP_AUTHORIZATION=f"Bearer {store_token}")
quality.credentials(HTTP_AUTHORIZATION=f"Bearer {quality_token}")

admin_user = User.objects.get(username="admin_user")
store_user = User.objects.get(username="store_user")
quality_user = User.objects.get(username="quality_user")

# ══════════════════════════════════════════════════════════════════════════
# 1. DEPARTMENT BUDGET
# ══════════════════════════════════════════════════════════════════════════
section("1. DEPARTMENT BUDGET MANAGEMENT")

# Clean slate
DepartmentBudget.objects.all().delete()

# Admin can create budget
r = admin.post("/api/finance/budgets/", {
    "department": "inventory",
    "period": "monthly",
    "period_label": "Feb 2026",
    "total_budget": "50000.00",
    "auto_approve_limit": "5000.00",
}, format="json")
check(r.status_code == 201, "Admin creates Inventory budget (Feb 2026, $50,000)")
budget_id = r.data.get("id") if r.status_code == 201 else None

# Non-admin cannot create
r2 = store.post("/api/finance/budgets/", {
    "department": "inventory", "period": "monthly",
    "period_label": "Feb 2026", "total_budget": "10000"
}, format="json")
check(r2.status_code == 403, "store_user CANNOT create budget (403)")

# Anyone can list
r3 = store.get("/api/finance/budgets/")
check(r3.status_code == 200, "store_user CAN read budget list")

# Admin creates 2nd budget for quality
r4 = admin.post("/api/finance/budgets/", {
    "department": "quality", "period": "monthly",
    "period_label": "Feb 2026", "total_budget": "20000.00",
    "auto_approve_limit": "2000.00",
}, format="json")
check(r4.status_code == 201, "Admin creates Quality budget (Feb 2026, $20,000)")
quality_budget_id = r4.data.get("id") if r4.status_code == 201 else None

# Verify computed fields
if budget_id:
    r5 = admin.get(f"/api/finance/budgets/{budget_id}/")
    check(r5.status_code == 200, "Admin reads budget detail")
    check(r5.data.get("spent") == 0.0, "  → spent = $0 initially")
    check(r5.data.get("remaining") == 50000.0, "  → remaining = $50,000")
    check(r5.data.get("utilization_pct") == 0.0, "  → utilization = 0%")

# ══════════════════════════════════════════════════════════════════════════
# 2. EXPENSE REQUESTS – AUTO-APPROVAL LOGIC
# ══════════════════════════════════════════════════════════════════════════
section("2. EXPENSE REQUESTS & AUTO-APPROVAL LOGIC")

ExpenseRequest.objects.all().delete()

# store_user submits $3,000 expense → BELOW $5,000 auto-limit → AUTO-APPROVED
r = store.post("/api/finance/expenses/", {
    "title": "Sugar bulk purchase",
    "category": "raw_material",
    "amount": "3000.00",
    "budget": budget_id,
    "vendor": "SweetSource Inc.",
}, format="json")
check(r.status_code == 201, "store_user submits $3,000 expense (within auto-approve limit)")
check(r.data.get("status") == "auto_approved", "  → Status = auto_approved ✨")
small_expense_id = r.data.get("id")

# store_user submits $8,000 expense → ABOVE $5,000 limit → PENDING
r2 = store.post("/api/finance/expenses/", {
    "title": "Packaging machine parts",
    "category": "equipment",
    "amount": "8000.00",
    "budget": budget_id,
    "vendor": "MachineWorld",
}, format="json")
check(r2.status_code == 201, "store_user submits $8,000 expense (above auto-approve limit)")
check(r2.data.get("status") == "pending", "  → Status = pending (needs admin approval)")
large_expense_id = r2.data.get("id")

# Non-admin sees only their own requests
r3 = store.get("/api/finance/expenses/")
check(r3.status_code == 200, "store_user can list their own expenses")
if r3.status_code == 200:
    own_only = all(e["requested_by"] == store_user.id for e in r3.data)
    check(own_only, "  → Only own expenses visible to non-admin")

# Admin sees ALL
r4 = admin.get("/api/finance/expenses/")
check(r4.status_code == 200 and len(r4.data) >= 2, "Admin sees ALL expenses")

# Admin can see pending approvals endpoint
r5 = admin.get("/api/finance/expenses/pending_approvals/")
check(r5.status_code == 200, "Admin can access pending_approvals endpoint")
if r5.status_code == 200:
    check(any(e["id"] == large_expense_id for e in r5.data), "  → $8,000 pending expense is in list")

# store_user CANNOT approve
if large_expense_id:
    r6 = store.post(f"/api/finance/expenses/{large_expense_id}/approve/",
                    {"status": "approved"}, format="json")
    check(r6.status_code == 403, "store_user CANNOT approve expense (403)")

# Admin approves the $8,000 request
if large_expense_id:
    r7 = admin.post(f"/api/finance/expenses/{large_expense_id}/approve/",
                    {"status": "approved", "notes": "Approved – within Q1 plan"}, format="json")
    check(r7.status_code == 200, "Admin approves $8,000 expense")
    check(r7.data.get("status") == "approved", "  → Status = approved")
    check(r7.data.get("notes") == "Approved – within Q1 plan", "  → Admin note saved")

# Verify budget spent/remaining updated
if budget_id:
    r8 = admin.get(f"/api/finance/budgets/{budget_id}/")
    spent = r8.data.get("spent")
    check(spent == 11000.0, f"  → Budget spent updated to $11,000 (was $0) = {spent}")
    check(r8.data.get("remaining") == 39000.0, "  → Remaining = $39,000")

# Admin rejects a new request
r9 = quality.post("/api/finance/expenses/", {
    "title": "Lab equipment upgrade",
    "category": "equipment",
    "amount": "15000.00",
    "budget": quality_budget_id,
    "vendor": "LabTech Pro",
}, format="json")
if r9.status_code == 201:
    rej_id = r9.data.get("id")
    r10 = admin.post(f"/api/finance/expenses/{rej_id}/approve/",
                     {"status": "rejected", "notes": "Not in this quarter's plan"}, format="json")
    check(r10.status_code == 200, "Admin rejects $15,000 quality expense")
    check(r10.data.get("status") == "rejected", "  → Status = rejected")

# ══════════════════════════════════════════════════════════════════════════
# 3. PAYROLL MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════
section("3. PAYROLL MANAGEMENT")

PayrollRecord.objects.all().delete()

# Admin creates payroll record
r = admin.post("/api/finance/payroll/", {
    "employee": store_user.id,
    "period_label": "February 2026",
    "basic_salary": "45000.00",
    "allowances": "5000.00",
    "overtime_pay": "2000.00",
    "deductions": "1500.00",
    "tax": "6750.00",
}, format="json")
check(r.status_code == 201, "Admin creates payroll for store_user (Feb 2026)")
if r.status_code == 201:
    expected_net = 45000 + 5000 + 2000 - 1500 - 6750  # = 43750
    check(float(r.data.get("net_salary")) == expected_net,
          f"  → Net salary auto-calculated = ${expected_net:,.2f}")
    check(r.data.get("pay_status") == "pending", "  → Initial status = pending")
    pr_id = r.data.get("id")

    # Process payroll
    r2 = admin.post(f"/api/finance/payroll/{pr_id}/process_payroll/")
    check(r2.status_code == 200, "Admin processes payroll → 'processed'")
    check(r2.data.get("pay_status") == "processed", "  → Status = processed")

    # Mark as paid
    r3 = admin.post(f"/api/finance/payroll/{pr_id}/mark_paid/")
    check(r3.status_code == 200, "Admin marks payroll as paid")
    check(r3.data.get("pay_status") == "paid", "  → Status = paid")
    check(r3.data.get("payment_date") is not None, "  → payment_date recorded")

# Non-admin cannot create payroll
r4 = store.post("/api/finance/payroll/", {
    "employee": store_user.id, "period_label": "February 2026",
    "basic_salary": "50000", "net_salary": "50000",
}, format="json")
check(r4.status_code == 403, "store_user CANNOT create payroll (403)")

# Non-admin sees only their own payroll
r5 = store.get("/api/finance/payroll/")
check(r5.status_code == 200, "store_user can read their own payroll")
if r5.status_code == 200:
    own_only = all(p["employee"] == store_user.id for p in r5.data)
    check(own_only, "  → Only own payroll visible to non-admin")

# Payroll summary
r6 = admin.get("/api/finance/payroll/summary/")
check(r6.status_code == 200, "Admin gets payroll summary")
if r6.status_code == 200:
    check(r6.data.get("count", 0) >= 1, f"  → Records counted = {r6.data.get('count')}")

# ══════════════════════════════════════════════════════════════════════════
# 4. OPERATIONAL COSTS
# ══════════════════════════════════════════════════════════════════════════
section("4. OPERATIONAL COSTS")

OperationalCost.objects.all().delete()

r = admin.post("/api/finance/operational-costs/", {
    "title": "March Electricity Bill",
    "cost_type": "fixed",
    "department": "production",
    "amount": "8500.00",
    "date": "2026-02-28",
    "vendor": "City Power Corp",
    "invoice_number": "INV-2026-0301",
}, format="json")
check(r.status_code == 201, "Admin records fixed operational cost ($8,500 electricity)")

r2 = admin.post("/api/finance/operational-costs/", {
    "title": "Water usage charges",
    "cost_type": "variable",
    "department": "production",
    "amount": "2200.00",
    "date": "2026-02-28",
}, format="json")
check(r2.status_code == 201, "Admin records variable cost ($2,200 water)")

r3 = store.post("/api/finance/operational-costs/", {
    "title": "Office supplies",
    "cost_type": "one_time",
    "department": "inventory",
    "amount": "500.00",
    "date": "2026-02-28",
}, format="json")
check(r3.status_code == 403, "store_user CANNOT record costs (403)")

r4 = store.get("/api/finance/operational-costs/")
check(r4.status_code == 200, "store_user CAN read costs")

# Filter by department
r5 = admin.get("/api/finance/operational-costs/?department=production")
check(r5.status_code == 200 and len(r5.data) >= 2, "Filter costs by department=production")

# ══════════════════════════════════════════════════════════════════════════
# 5. FINANCIAL DASHBOARD KPIs
# ══════════════════════════════════════════════════════════════════════════
section("5. FINANCIAL DASHBOARD KPIs")

r = admin.get("/api/finance/summaries/dashboard/")
check(r.status_code == 200, "Admin accesses dashboard KPI endpoint")
if r.status_code == 200:
    check("total_budget" in r.data, "  → total_budget present")
    check("total_spent" in r.data, "  → total_spent present")
    check("pending_approvals" in r.data, "  → pending_approvals present")
    check("total_payroll_paid" in r.data, "  → total_payroll_paid present")
    check(r.data["total_budget"] > 0, f"  → total_budget = ${r.data['total_budget']:,.2f}")
    print(f"         pending_approvals = {r.data['pending_approvals']}")
    print(f"         total_payroll_paid = ${r.data['total_payroll_paid']:,.2f}")

# ══════════════════════════════════════════════════════════════════════════
# 6. USERS LIST (for payroll admin)
# ══════════════════════════════════════════════════════════════════════════
section("6. USERS LIST ENDPOINT")

r = admin.get("/api/auth/users/")
check(r.status_code == 200, "Admin can list all users")
if r.status_code == 200:
    check(len(r.data) >= 3, f"  → {len(r.data)} users returned")

r2 = store.get("/api/auth/users/")
check(r2.status_code == 403, "store_user CANNOT list all users (403)")

# ──────────────────────────────────────────────────────────────────────────
print(f"\n{'═'*55}")
total = results['pass'] + results['fail']
print(f"  RESULT: {results['pass']}/{total} tests passed", end="  ")
if results['fail'] == 0:
    print("\033[92m🎉 ALL TESTS PASSED!\033[0m")
else:
    print(f"\033[91m⚠  {results['fail']} failed\033[0m")
print(f"{'═'*55}\n")
