
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
    SafetyIncident, PayrollRecord, WorkforceNotification
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

# 1. SHIFT MANAGEMENT
section("1. SHIFT MANAGEMENT")
shift = Shift.objects.filter(shift_type="morning").first()
emp = Employee.objects.filter(user=prod_user).first()

if not shift:
    shift = Shift.objects.create(name="Default Morning", shift_type="morning", start_time="08:00", end_time="16:00")

r = hr_client.post(f"/api/workforce/employees/{emp.id}/assign_shift/", {
    "shift_id": shift.id,
    "date": str(date.today())
}, format="json")
check(r.status_code == 200, "HR can assign shift to employee")

r = hr_client.get("/api/workforce/shift-assignments/weekly_roster/")
check(r.status_code == 200, "HR can access weekly roster")

# 2. SAFETY INCIDENTS
section("2. SAFETY INCIDENTS")
r = hr_client.post("/api/workforce/safety-incidents/", {
    "title": "Slippery floor",
    "description": "Floor near Line A is wet",
    "employee": emp.id,
    "department": emp.department.id,
    "severity": "medium",
    "incident_date": timezone.now().isoformat()
}, format="json")
check(r.status_code == 201, "HR can report safety incident")
incident_id = r.data.get('id')

if incident_id:
    r = hr_client.post(f"/api/workforce/safety-incidents/{incident_id}/resolve/", {"notes": "Cleaned up and sign placed"}, format="json")
    check(r.status_code == 200, "HR can resolve safety incident")
    check(r.data.get('status') == 'resolved', "  → Status is 'resolved'")

# 3. NOTIFICATIONS
section("3. NOTIFICATIONS")
r = hr_client.post("/api/workforce/notifications/broadcast/", {
    "title": "System Update",
    "message": "The system will be down for maintenance at 10 PM.",
    "type": "general"
}, format="json")
check(r.status_code == 200, "HR can broadcast notification")
check(r.data.get('sent', 0) > 0, f"  → {r.data.get('sent')} notifications sent")

# 4. ATTENDANCE CORRECTION
section("4. ATTENDANCE CORRECTION")
att_record = AttendanceRecord.objects.get_or_create(employee=emp, date=date.today())[0]
r = hr_client.patch(f"/api/workforce/attendance/{att_record.id}/correct/", {
    "status": "present",
    "notes": "Manual correction by HR"
}, format="json")
check(r.status_code == 200, "HR can manually correct attendance")

# 5. EMPLOYEE DEACTIVATION
section("5. EMPLOYEE DEACTIVATION")
# Create a temporary employee to deactivate
temp_user = User.objects.create_user(username="temp_emp", password="password123", role="production")
temp_emp = Employee.objects.create(
    user=temp_user, first_name="Temp", last_name="Emp", email="temp@example.com",
    employee_id="EMP_TEMP", department=emp.department
)
r = hr_client.post(f"/api/workforce/employees/{temp_emp.id}/deactivate/", {"reason": "resigned"}, format="json")
check(r.status_code == 200, "HR can deactivate employee")
temp_emp.refresh_from_db()
check(temp_emp.status == "resigned", "  → Status updated to 'resigned'")

# Cleanup
temp_user.delete()

print(f"\n{'═'*60}")
total = results['pass'] + results['fail']
print(f"  RESULT: {results['pass']}/{total} tests passed")
print(f"{'═'*60}\n")
