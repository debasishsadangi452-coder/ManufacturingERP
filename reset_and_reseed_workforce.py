"""
Resets the workforce data to only include the 5 required departments.
"""
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from workforce.models import Department, JobRole, LeaveType, Shift, Skill, Employee, EmployeeDocument, ShiftAssignment, AttendanceRecord, LeaveBalance, LeaveRequest, EmployeeSkill, TrainingProgram, SafetyIncident, PayrollRecord, WorkforceNotification

print("Clearing all workforce data...")

# Deleting in reverse dependency order
WorkforceNotification.objects.all().delete()
PayrollRecord.objects.all().delete()
SafetyIncident.objects.all().delete()
TrainingProgram.objects.all().delete()
EmployeeSkill.objects.all().delete()
LeaveRequest.objects.all().delete()
LeaveBalance.objects.all().delete()
AttendanceRecord.objects.all().delete()
ShiftAssignment.objects.all().delete()
EmployeeDocument.objects.all().delete()
Employee.objects.all().delete()
Skill.objects.all().delete()
Shift.objects.all().delete()
LeaveType.objects.all().delete()
JobRole.objects.all().delete()
Department.objects.all().delete()

print("Workforce data cleared.")

# ── New Departments ────────────────────────────────────────────────────────
new_depts = [
    {"name": "Admin",      "code": "admin", "description": "Admin department"},
    {"name": "Store",      "code": "store", "description": "Store and warehouse department"},
    {"name": "Production", "code": "production", "description": "Production and manufacturing"},
    {"name": "Sales",      "code": "sales", "description": "Sales and marketing"},
    {"name": "Quality",    "code": "quality", "description": "Quality control and assurance"},
]

for d in new_depts:
    obj, created = Department.objects.get_or_create(code=d["code"], defaults=d)
    print(f"Created Department: {obj.name}")

# ── Base Job Roles ────────────────────────────────────────────────────────
base_roles = [
    ("Admin Manager", "admin", "admin"),
    ("Store Manager", "store", "store"),
    ("Production Manager", "production", "production"),
    ("Sales Manager", "sales", "sales"),
    ("Quality Manager", "quality", "quality"),
    ("Operator", "production", "production"),
    ("QC Inspector", "quality", "quality"),
    ("Store Clerk", "store", "store"),
]

for name, dept_code, erp_role in base_roles:
    dept = Department.objects.get(code=dept_code)
    obj, created = JobRole.objects.get_or_create(name=name, department=dept, defaults={"erp_role": erp_role})
    print(f"Created Role: {obj.name} ({dept.name})")

# ── Re-seed Leave Types ──────────────────────────────────────────────────
leave_types = [
    {"name": "Annual Leave",    "code": "AL",  "annual_quota": 21, "is_paid": True,  "carry_forward": True},
    {"name": "Sick Leave",      "code": "SL",  "annual_quota": 10, "is_paid": True,  "carry_forward": False},
    {"name": "Casual Leave",    "code": "CL",  "annual_quota": 7,  "is_paid": True,  "carry_forward": False},
]
for lt in leave_types:
    obj, created = LeaveType.objects.get_or_create(code=lt["code"], defaults=lt)

# ── Re-seed Shifts ───────────────────────────────────────────────────────
shifts = [
    {"name": "Morning", "shift_type": "morning", "start_time": "09:00", "end_time": "18:00", "capacity": 50},
]
for sh in shifts:
    obj, created = Shift.objects.get_or_create(name=sh["name"], defaults=sh)

print("\n✅ New Workforce setup complete!")
