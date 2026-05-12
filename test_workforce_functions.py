
import os, django, json, sys
from datetime import date, timedelta
from django.utils import timezone
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from rest_framework.test import APIClient
from accounts.models import User
from workforce.models import (
    Employee, Department, JobRole, Shift, ShiftAssignment,
    AttendanceRecord, LeaveType, LeaveRequest, Skill, TrainingProgram,
    SafetyIncident, PayrollRecord
)

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
    print(f"\n{HEAD}{'─'*60}{RESET}")
    print(f"{HEAD} {title}{RESET}")
    print(f"{HEAD}{'─'*60}{RESET}")

def check(cond, name, detail=""):
    if cond:
        ok(name)
    else:
        fail(name, detail)

# Setup
admin_user = User.objects.get(username="admin_user")
hr_user = User.objects.get(username="hr_user")
prod_user = User.objects.get(username="production_user")

def get_token(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return str(RefreshToken.for_user(user).access_token)

admin_client = APIClient()
admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {get_token(admin_user)}")

hr_client = APIClient()
hr_client.credentials(HTTP_AUTHORIZATION=f"Bearer {get_token(hr_user)}")

prod_client = APIClient()
prod_client.credentials(HTTP_AUTHORIZATION=f"Bearer {get_token(prod_user)}")

# 1. Attendance & Clock-in/out
section("1. ATTENDANCE & CLOCK-IN/OUT")
# Get or create employee for production_user
dept = Department.objects.first()
job_role = JobRole.objects.filter(department=dept).first()
emp, _ = Employee.objects.get_or_create(
    user=prod_user,
    defaults={
        "first_name": "Prod", "last_name": "User", "email": "prod@example.com",
        "employee_id": "EMP_PROD", "department": dept, "job_role": job_role
    }
)

# Clock in via self-service
r = prod_client.post("/api/workforce/me/attendance/", {"action": "clock_in"}, format="json")
check(r.status_code == 200, "Employee can clock in")

# Clock out via self-service
r = prod_client.post("/api/workforce/me/attendance/", {"action": "clock_out"}, format="json")
check(r.status_code == 200, "Employee can clock out")

# 2. Leave Requests
section("2. LEAVE REQUESTS")
leave_type = LeaveType.objects.first()
r = prod_client.post("/api/workforce/me/leave/", {
    "leave_type": leave_type.id,
    "start_date": str(date.today() + timedelta(days=5)),
    "end_date": str(date.today() + timedelta(days=7)),
    "reason": "Family function"
}, format="json")
check(r.status_code == 201, "Employee can request leave")
leave_id = r.data.get('id')

# HR approves leave
if leave_id:
    r = hr_client.post(f"/api/workforce/leave-requests/{leave_id}/approve/")
    check(r.status_code == 200, "HR can approve leave")
    check(r.data.get('status') == 'approved', "  → Status is 'approved'")

# 3. Training
section("3. TRAINING PROGRAMS")
r = hr_client.post("/api/workforce/training/", {
    "name": "Safety 101", "program_type": "safety", "due_date": str(date.today() + timedelta(days=30)),
    "mandatory": True, "status": "planned"
}, format="json")
check(r.status_code == 201, "HR can create training program")
training_id = r.data.get('id')

if training_id:
    r = hr_client.post(f"/api/workforce/training/{training_id}/enroll/", {"employee_ids": [emp.id]}, format="json")
    check(r.status_code == 200, "HR can enroll employee in training")

# 4. Payroll Generation
section("4. PAYROLL GENERATION")
r = hr_client.post("/api/workforce/payroll/generate/", {"month": date.today().month, "year": date.today().year}, format="json")
check(r.status_code == 200, "HR can generate payroll records")

# 5. Dashboard
section("5. DASHBOARD")
r = hr_client.get("/api/workforce/dashboard/")
check(r.status_code == 200, "HR can access workforce dashboard")
if r.status_code == 200:
    check("total_employees" in r.data, "  → Found total_employees KPI")

print(f"\n{'═'*60}")
total = results['pass'] + results['fail']
print(f"  RESULT: {results['pass']}/{total} tests passed")
print(f"{'═'*60}\n")
