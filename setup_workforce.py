"""
Updated setup script for Workforce module with 5 core departments.
Run: python setup_workforce.py
"""
import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freshfizz_erp.settings')
django.setup()

from workforce.models import Department, JobRole, LeaveType, Shift, Skill

# ── Departments ────────────────────────────────────────────────────────────
departments = [
    {"name": "Admin",      "code": "admin", "description": "Admin department"},
    {"name": "Store",      "code": "store", "description": "Store and warehouse department"},
    {"name": "Production", "code": "production", "description": "Production and manufacturing"},
    {"name": "Sales",      "code": "sales", "description": "Sales and marketing"},
    {"name": "Quality",    "code": "quality", "description": "Quality control and assurance"},
]
for d in departments:
    obj, created = Department.objects.get_or_create(code=d["code"], defaults=d)
    print(f"{'Created' if created else 'Exists '} Department: {obj.name}")

# ── Job Roles ──────────────────────────────────────────────────────────────
roles = [
    ("Admin Manager",      "admin",      "admin"),
    ("System Admin",       "admin",      "admin"),
    ("Store Manager",      "store",      "store"),
    ("Warehouse Worker",   "store",      "store"),
    ("Production Manager", "production", "production"),
    ("Line Operator",      "production", "production"),
    ("Sales Manager",      "sales",      "sales"),
    ("Sales Associate",    "sales",      "sales"),
    ("Quality Manager",    "quality",    "quality"),
    ("QC Inspector",       "quality",    "quality"),
]
for name, dept_code, erp_role in roles:
    dept = Department.objects.get(code=dept_code)
    obj, created = JobRole.objects.get_or_create(name=name, department=dept, defaults={"erp_role": erp_role})
    print(f"{'Created' if created else 'Exists '} Role: {obj.name} ({dept.name})")

# ── Leave Types ────────────────────────────────────────────────────────────
leave_types = [
    {"name": "Annual Leave",    "code": "AL",  "annual_quota": 21, "is_paid": True,  "carry_forward": True},
    {"name": "Sick Leave",      "code": "SL",  "annual_quota": 10, "is_paid": True,  "carry_forward": False},
    {"name": "Casual Leave",    "code": "CL",  "annual_quota": 7,  "is_paid": True,  "carry_forward": False},
]
for lt in leave_types:
    obj, created = LeaveType.objects.get_or_create(code=lt["code"], defaults=lt)
    print(f"{'Created' if created else 'Exists '} Leave Type: {obj.name}")

# ── Shifts ─────────────────────────────────────────────────────────────────
shifts = [
    {"name": "Morning Shift",   "shift_type": "morning",   "start_time": "06:00", "end_time": "14:00", "capacity": 30, "status": "active"},
    {"name": "Afternoon Shift", "shift_type": "afternoon", "start_time": "14:00", "end_time": "22:00", "capacity": 25, "status": "upcoming"},
]
for sh in shifts:
    obj, created = Shift.objects.get_or_create(name=sh["name"], defaults=sh)
    print(f"{'Created' if created else 'Exists '} Shift: {obj.name}")

print("\n✅ Workforce setup complete with 5 core departments!")
