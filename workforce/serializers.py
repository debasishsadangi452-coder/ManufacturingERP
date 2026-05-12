from rest_framework import serializers
from .models import (
    Department, JobRole, Employee, EmployeeDocument,
    Shift, ShiftAssignment,
    AttendanceRecord,
    LeaveType, LeaveBalance, LeaveRequest,
    Skill, EmployeeSkill, TrainingProgram,
    SafetyIncident, PayrollRecord, WorkforceNotification,
)


# ─── Department & Role ─────────────────────────────────────────────────────

class DepartmentSerializer(serializers.ModelSerializer):
    employee_count = serializers.SerializerMethodField()
    manager_name = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = '__all__'

    def get_employee_count(self, obj):
        return obj.employees.filter(status='active').count()

    def get_manager_name(self, obj):
        return obj.manager.name if obj.manager else None


class JobRoleSerializer(serializers.ModelSerializer):
    department_name = serializers.ReadOnlyField(source='department.name')
    employee_count = serializers.SerializerMethodField()

    class Meta:
        model = JobRole
        fields = '__all__'

    def get_employee_count(self, obj):
        return obj.employees.count()


# ─── Employee ──────────────────────────────────────────────────────────────

class EmployeeListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for lists."""
    name = serializers.ReadOnlyField()
    department_name = serializers.ReadOnlyField(source='department.name')
    job_role_name = serializers.ReadOnlyField(source='job_role.name')
    manager_name = serializers.SerializerMethodField()

    username = serializers.ReadOnlyField(source='user.username')

    class Meta:
        model = Employee
        fields = [
            'id', 'employee_id', 'first_name', 'last_name', 'name', 'username',
            'email', 'phone', 'department', 'department_name',
            'job_role', 'job_role_name', 'role', 'shift', 'status',
            'employment_type', 'date_joined', 'assigned_line',
            'performance', 'attendance', 'safety_score',
            'manager', 'manager_name', 'photo',
        ]

    def get_manager_name(self, obj):
        return obj.manager.name if obj.manager else None


class EmployeeSerializer(serializers.ModelSerializer):
    """Full serializer for create/update/detail."""
    name = serializers.ReadOnlyField()
    department_name = serializers.ReadOnlyField(source='department.name')
    job_role_name = serializers.ReadOnlyField(source='job_role.name')
    manager_name = serializers.SerializerMethodField()
    skill_count = serializers.SerializerMethodField()
    certification_icons = serializers.SerializerMethodField()

    username = serializers.CharField(write_only=True, required=False)
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Employee
        fields = '__all__'
        read_only_fields = ['employee_id', 'created_at', 'updated_at']

    def create(self, validated_data):
        username = validated_data.pop('username', None)
        password = validated_data.pop('password', None)
        
        # Determine core role from JobRole or default to production
        job_role = validated_data.get('job_role')
        erp_role = job_role.erp_role if job_role else 'production'

        employee = super().create(validated_data)

        if username and password:
            from accounts.models import User
            # Check if user already exists
            if not User.objects.filter(username=username).exists():
                user = User.objects.create_user(
                    username=username,
                    password=password,
                    email=employee.email,
                    first_name=employee.first_name,
                    last_name=employee.last_name,
                    role=erp_role
                )
                employee.user = user
                employee.save()
        
        return employee

    def get_manager_name(self, obj):
        return obj.manager.name if obj.manager else None

    def get_skill_count(self, obj):
        return obj.skills.count()

    def get_certification_icons(self, obj):
        return list(obj.skills.filter(certified=True).values_list('skill__name', flat=True))


class EmployeeDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeDocument
        fields = '__all__'


# ─── Shift ─────────────────────────────────────────────────────────────────

class ShiftSerializer(serializers.ModelSerializer):
    supervisor_name = serializers.ReadOnlyField(source='supervisor.name')
    employee_count = serializers.SerializerMethodField()

    class Meta:
        model = Shift
        fields = '__all__'

    def get_employee_count(self, obj):
        return obj.assignments.filter(date=__import__('datetime').date.today()).count()


class ShiftAssignmentSerializer(serializers.ModelSerializer):
    employee_name = serializers.ReadOnlyField(source='employee.name')
    shift_name = serializers.ReadOnlyField(source='shift.name')

    class Meta:
        model = ShiftAssignment
        fields = '__all__'


# ─── Attendance ────────────────────────────────────────────────────────────

class AttendanceRecordSerializer(serializers.ModelSerializer):
    employee_name = serializers.ReadOnlyField(source='employee.name')
    employee_id_display = serializers.ReadOnlyField(source='employee.employee_id')
    department_name = serializers.ReadOnlyField(source='employee.department.name')

    class Meta:
        model = AttendanceRecord
        fields = '__all__'
        read_only_fields = ['working_hours', 'overtime_hours']


# ─── Leave ─────────────────────────────────────────────────────────────────

class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = '__all__'


class LeaveBalanceSerializer(serializers.ModelSerializer):
    employee_name = serializers.ReadOnlyField(source='employee.name')
    leave_type_name = serializers.ReadOnlyField(source='leave_type.name')
    remaining_days = serializers.ReadOnlyField()

    class Meta:
        model = LeaveBalance
        fields = '__all__'


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.ReadOnlyField(source='employee.name')
    employee_department = serializers.ReadOnlyField(source='employee.department.name')
    leave_type_name = serializers.ReadOnlyField(source='leave_type.name')
    approved_by_name = serializers.SerializerMethodField()
    duration_days = serializers.ReadOnlyField()

    class Meta:
        model = LeaveRequest
        fields = '__all__'
        read_only_fields = ['applied_on', 'updated_on', 'approved_by']

    def get_approved_by_name(self, obj):
        return obj.approved_by.get_full_name() if obj.approved_by else None


# ─── Skills & Training ─────────────────────────────────────────────────────

class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = '__all__'


class EmployeeSkillSerializer(serializers.ModelSerializer):
    skill_name = serializers.ReadOnlyField(source='skill.name')
    employee_name = serializers.ReadOnlyField(source='employee.name')

    class Meta:
        model = EmployeeSkill
        fields = '__all__'


class TrainingProgramSerializer(serializers.ModelSerializer):
    enrolled_count = serializers.IntegerField(source='enrolled_employees.count', read_only=True)
    completed_count = serializers.IntegerField(source='completed_employees.count', read_only=True)

    class Meta:
        model = TrainingProgram
        fields = '__all__'


# ─── Safety ────────────────────────────────────────────────────────────────

class SafetyIncidentSerializer(serializers.ModelSerializer):
    employee_name = serializers.ReadOnlyField(source='employee.name')
    department_name = serializers.ReadOnlyField(source='department.name')
    reported_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SafetyIncident
        fields = '__all__'

    def get_reported_by_name(self, obj):
        return obj.reported_by.get_full_name() if obj.reported_by else None


# ─── Payroll ───────────────────────────────────────────────────────────────

class PayrollRecordSerializer(serializers.ModelSerializer):
    employee_name = serializers.ReadOnlyField(source='employee.name')
    employee_id_display = serializers.ReadOnlyField(source='employee.employee_id')
    department_name = serializers.ReadOnlyField(source='employee.department.name')

    class Meta:
        model = PayrollRecord
        fields = '__all__'


# ─── Notifications ─────────────────────────────────────────────────────────

class WorkforceNotificationSerializer(serializers.ModelSerializer):
    recipient_name = serializers.ReadOnlyField(source='recipient.name')

    class Meta:
        model = WorkforceNotification
        fields = '__all__'


# ─── Dashboard Summary ─────────────────────────────────────────────────────

class WorkforceDashboardSerializer(serializers.Serializer):
    total_employees = serializers.IntegerField()
    active_today = serializers.IntegerField()
    on_leave = serializers.IntegerField()
    in_training = serializers.IntegerField()
    resigned = serializers.IntegerField()
    pending_leave_requests = serializers.IntegerField()
    open_incidents = serializers.IntegerField()
    upcoming_trainings = serializers.IntegerField()
    department_breakdown = serializers.ListField()
    shift_summary = serializers.ListField()
    attendance_rate = serializers.FloatField()
    overtime_hours_this_month = serializers.FloatField()
