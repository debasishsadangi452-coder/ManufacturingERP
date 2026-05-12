from django.contrib import admin
from .models import (
    Department, JobRole, Employee, EmployeeDocument,
    Shift, ShiftAssignment,
    AttendanceRecord,
    LeaveType, LeaveBalance, LeaveRequest,
    Skill, EmployeeSkill, TrainingProgram,
    SafetyIncident, PayrollRecord, WorkforceNotification,
)

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'manager']
    search_fields = ['name', 'code']

@admin.register(JobRole)
class JobRoleAdmin(admin.ModelAdmin):
    list_display = ['name', 'department', 'erp_role']
    list_filter = ['department', 'erp_role']

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ['employee_id', 'first_name', 'last_name', 'department', 'role', 'status', 'shift']
    list_filter = ['status', 'shift', 'department', 'employment_type']
    search_fields = ['first_name', 'last_name', 'email', 'employee_id']

@admin.register(EmployeeDocument)
class EmployeeDocumentAdmin(admin.ModelAdmin):
    list_display = ['employee', 'doc_type', 'name', 'expiry_date']
    list_filter = ['doc_type']

@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ['name', 'shift_type', 'start_time', 'end_time', 'capacity', 'status']
    list_filter = ['shift_type', 'status']

@admin.register(ShiftAssignment)
class ShiftAssignmentAdmin(admin.ModelAdmin):
    list_display = ['employee', 'shift', 'date']
    list_filter = ['date', 'shift']

@admin.register(AttendanceRecord)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ['employee', 'date', 'status', 'check_in', 'check_out', 'working_hours', 'overtime_hours']
    list_filter = ['status', 'date']
    search_fields = ['employee__first_name', 'employee__last_name']

@admin.register(LeaveType)
class LeaveTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'annual_quota', 'is_paid']

@admin.register(LeaveBalance)
class LeaveBalanceAdmin(admin.ModelAdmin):
    list_display = ['employee', 'leave_type', 'year', 'total_days', 'used_days']

@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ['employee', 'leave_type', 'start_date', 'end_date', 'status', 'applied_on']
    list_filter = ['status', 'leave_type']

@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ['name', 'category']

@admin.register(EmployeeSkill)
class EmployeeSkillAdmin(admin.ModelAdmin):
    list_display = ['employee', 'skill', 'level', 'certified', 'expiry_date']
    list_filter = ['certified', 'level']

@admin.register(TrainingProgram)
class TrainingProgramAdmin(admin.ModelAdmin):
    list_display = ['name', 'program_type', 'due_date', 'mandatory', 'status']
    list_filter = ['program_type', 'mandatory', 'status']

@admin.register(SafetyIncident)
class SafetyIncidentAdmin(admin.ModelAdmin):
    list_display = ['title', 'severity', 'status', 'incident_date', 'employee']
    list_filter = ['severity', 'status']

@admin.register(PayrollRecord)
class PayrollRecordAdmin(admin.ModelAdmin):
    list_display = ['employee', 'month', 'year', 'net_pay', 'total_working_hours']
    list_filter = ['month', 'year']

@admin.register(WorkforceNotification)
class WorkforceNotificationAdmin(admin.ModelAdmin):
    list_display = ['title', 'notification_type', 'recipient', 'is_read', 'created_at']
    list_filter = ['notification_type', 'is_read', 'is_broadcast']
