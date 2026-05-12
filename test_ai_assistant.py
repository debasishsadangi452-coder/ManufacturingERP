
import os, django, json, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from rest_framework.test import APIClient
from accounts.models import User
from inventory.models import Item, Warehouse, Stock

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
HEAD = "\033[94m"
RESET = "\033[0m"

results = {"pass": 0, "fail": 0}

def check(cond, name, detail=""):
    if cond:
        print(f"   {PASS}  {name}")
        results["pass"] += 1
    else:
        print(f"   {FAIL}  {name}" + (f" → {detail}" if detail else ""))
        results["fail"] += 1
        print(f"      DEBUG: {detail}")

def section(title):
    print(f"\n{HEAD}{'─'*60}{RESET}")
    print(f"{HEAD} {title}{RESET}")
    print(f"{HEAD}{'─'*60}{RESET}")

# Setup clients
def get_token(role):
    from rest_framework_simplejwt.tokens import RefreshToken
    user = User.objects.get(username=f"{role}_user")
    return str(RefreshToken.for_user(user).access_token)

admin_token = get_token('admin')
admin_client = APIClient()
admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {admin_token}")

section("AI ASSISTANT ENDPOINT TESTS")

# 1. Test InsightsView
r = admin_client.get("/api/ai/insights/")
check(r.status_code == 200, "InsightsView returns 200 OK")
if r.status_code == 200:
    data = r.json()
    check("insights" in data, "InsightsView returns 'insights' key")
    check(isinstance(data["insights"], list), "Insights are returned as a list")
    if data["insights"]:
        check("title" in data["insights"][0], "Insight has title")
        check("prediction" in data["insights"][0], "Insight has prediction")

# 2. Test ChatView (authenticated)
r = admin_client.post("/api/ai/chat/", {"message": "Hello"}, format="json")
check(r.status_code == 200, "ChatView returns 200 OK")
if r.status_code == 200:
    data = r.json()
    check("response" in data, "ChatView returns 'response' key")

# 3. Test Authentication
anon_client = APIClient()
r = anon_client.get("/api/ai/insights/")
check(r.status_code == 401, "Anonymous user cannot access insights")

section("AI TOOLS (FUNCTION LOGIC) TESTS")
from ai_assistant.tools import adjust_stock, get_inventory_summary

# Setup test data
item, _ = Item.objects.get_or_create(name="Test Item", category="raw_material")
warehouse, _ = Warehouse.objects.get_or_create(name="Test Warehouse")
stock, _ = Stock.objects.get_or_create(item=item, warehouse=warehouse)
initial_qty = stock.quantity

admin_user = User.objects.get(username="admin_user")
sales_user = User.objects.get(username="sales_user")

# Test adjust_stock tool logic directly
res = json.loads(adjust_stock(admin_user, item.id, warehouse.id, 10))
check(res.get("success") is True, "admin_user can adjust stock via tool")

res = json.loads(adjust_stock(sales_user, item.id, warehouse.id, 10))
check("error" in res, "sales_user UNAUTHORIZED to adjust stock via tool")

# Test get_inventory_summary
res = json.loads(get_inventory_summary(admin_user, "Test Item"))
check("Test Item" in res, "admin_user can see inventory summary")

section("FINAL SUMMARY")
print(f"Passed: {results['pass']}, Failed: {results['fail']}")
if results['fail'] == 0:
    print("🎉 ALL AI INTEGRATION TESTS PASSED")
else:
    print("⚠ SOME TESTS FAILED")
