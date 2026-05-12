
import os, django, json, sys
from datetime import date, timedelta
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from rest_framework.test import APIClient
from accounts.models import User
from finance.models import DepartmentBudget, ExpenseRequest, PayrollRecord as FinancePayroll
from workforce.models import Employee, Department as WorkforceDept, JobRole, LeaveType, LeaveRequest

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
HEAD = "\033[94m"
RESET = "\033[0m"
results = {"pass": 0, "fail": 0, "failures": []}

def ok(name):
    print(f"   {PASS}  {name}")
    results["pass"] += 1

def fail(name, detail=""):
    print(f"   {FAIL}  {name}" + (f" → {detail}" if detail else ""))
    results["fail"] += 1
    results["failures"].append({"test": name, "detail": str(detail)})

def section(title):
    print(f"\n{HEAD}{'─'*60}{RESET}")
    print(f"{HEAD} {title}{RESET}")
    print(f"{HEAD}{'─'*60}{RESET}")

def check(cond, name, detail=""):
    if cond:
        ok(name)
    else:
        fail(name, detail)

# ─── Setup clients ─────────────────────────────────────────────────────────
roles = ['admin', 'hr', 'store', 'production', 'quality', 'sales', 'finance']
clients = {}

def get_token(username, password="password123"):
    from rest_framework_simplejwt.tokens import RefreshToken
    try:
        user = User.objects.get(username=username)
        tokens = RefreshToken.for_user(user)
        return str(tokens.access_token)
    except User.DoesNotExist:
        return None

for role in roles:
    username = f"{role}_user"
    token = get_token(username)
    if not token:
        print(f"❌ {username} not found. Run: python setup_roles.py")
        sys.exit(1)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    clients[role] = client

admin = clients['admin']

# ══════════════════════════════════════════════════════════════════════════
# FINANCE MODULE TESTING
# ══════════════════════════════════════════════════════════════════════════
section("FINANCE MODULE ACCESS CONTROL")

# 1. Budgets: Admin CRUD, others Read
r = admin.get("/api/finance/budgets/")
check(r.status_code == 200, "Admin can read budgets")

for role in [r for r in roles if r != 'admin']:
    r = clients[role].get("/api/finance/budgets/")
    check(r.status_code == 200, f"{role}_user can read budgets")
    
    # Try to create
    r = clients[role].post("/api/finance/budgets/", {
        "department": "sales", "period": "monthly", "period_label": f"test-{role}", "total_budget": "100"
    }, format="json")
    if role == 'finance':
        check(r.status_code == 201, "finance_user CAN create budget", r.content)
    else:
        check(r.status_code == 403, f"{role}_user CANNOT create budget")

# 2. Expenses: All can submit, only Admin can approve
# Create a budget first
budget_r = admin.post("/api/finance/budgets/", {
    "department": "production", "period": "monthly", "period_label": "Mar 2026",
    "total_budget": "10000.00", "auto_approve_limit": "1000.00"
}, format="json")
budget_id = budget_r.data.get('id')

if budget_id:
    for role in roles:
        r = clients[role].post("/api/finance/expenses/", {
            "title": f"Test Expense {role}", "category": "other", "amount": "500.00",
            "budget": budget_id
        }, format="json")
        check(r.status_code == 201, f"{role}_user can submit expense")
        exp_id = r.data.get('id')
        
        # Non-admin/finance cannot approve
        if role not in ['admin', 'finance']:
            r_app = clients[role].post(f"/api/finance/expenses/{exp_id}/approve/", {"status": "approved"}, format="json")
            check(r_app.status_code == 403, f"{role}_user CANNOT approve expense")
        else:
            r_app = clients[role].post(f"/api/finance/expenses/{exp_id}/approve/", {"status": "approved"}, format="json")
            check(r_app.status_code == 200, f"{role}_user CAN approve expense")

# 3. Payroll: Admin CRUD, others own records
for role in roles:
    r = clients[role].get("/api/finance/payroll/")
    check(r.status_code == 200, f"{role}_user can access payroll endpoint")

# ══════════════════════════════════════════════════════════════════════════
# WORKFORCE MODULE TESTING
# ══════════════════════════════════════════════════════════════════════════
section("WORKFORCE MODULE ACCESS CONTROL")

workforce_endpoints = [
    "departments", "job-roles", "employees", "shifts", "attendance", 
    "leave-requests", "dashboard"
]

# HR and Admin should have full access
for role in ['admin', 'hr']:
    for ep in workforce_endpoints:
        url = f"/api/workforce/{ep}/"
        r = clients[role].get(url)
        check(r.status_code == 200, f"{role}_user can access workforce {ep}")

# Others should be forbidden for main endpoints
restricted_endpoints = [
    "departments", "job-roles", "employees", "shifts", "attendance", 
    "leave-balances", "payroll", "dashboard"
]

for role in [r for r in roles if r not in ['admin', 'hr']]:
    for ep in restricted_endpoints:
        url = f"/api/workforce/{ep}/"
        r = clients[role].get(url)
        check(r.status_code == 403, f"{role}_user FORBIDDEN from {ep}")

# Self-service endpoints should be accessible to all
self_service = ["me/profile", "me/attendance", "me/leave", "me/shift", "me/notifications"]
# But we need an Employee object for the user to exist
# Create employee objects for each user if they don't exist
for role in roles:
    user = User.objects.get(username=f"{role}_user")
    if not Employee.objects.filter(user=user).exists():
        dept = WorkforceDept.objects.first()
        Employee.objects.create(
            user=user, first_name=role, last_name="User", 
            email=f"{role}@example.com", employee_id=f"EMP_{role.upper()}",
            department=dept
        )

for role in roles:
    for ep in self_service:
        url = f"/api/workforce/{ep}/"
        r = clients[role].get(url)
        check(r.status_code == 200, f"{role}_user can access self-service {ep}")

# ══════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*60}")
total = results['pass'] + results['fail']
print(f"  RESULT: {results['pass']}/{total} tests passed", end="  ")
if results['fail'] == 0:
    print("\033[92m🎉 ALL ACCESS CONTROL TESTS PASSED!\033[0m")
else:
    print(f"\033[91m⚠  {results['fail']} failed\033[0m")
print(f"{'═'*60}\n")

with open('test_results.json', 'w') as f:
    json.dump(results, f, indent=2)
