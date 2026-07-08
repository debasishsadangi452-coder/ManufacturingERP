from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import time


class Department(models.Model):
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['company', 'name'], name='uniq_dept_name_per_company'),
            models.UniqueConstraint(fields=['company', 'code'], name='uniq_dept_code_per_company'),
        ]

    description = models.TextField(blank=True)
    manager = models.ForeignKey(
        'Employee', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='managed_departments'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']


class JobRole(models.Model):
    ERP_ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('store', 'Store'),
        ('production', 'Production'),
        ('sales', 'Sales'),
        ('quality', 'Quality'),
    ]
    name = models.CharField(max_length=100)
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='roles')
    erp_role = models.CharField(max_length=30, choices=ERP_ROLE_CHOICES, default='readonly')
    description = models.TextField(blank=True)
    permissions = models.JSONField(default=dict, help_text="Module-level access permissions")

    def __str__(self):
        return f"{self.name} ({self.department.name})"

    class Meta:
        unique_together = ('name', 'department')


class Employee(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('on-leave', 'On Leave'),
        ('training', 'In Training'),
        ('off-duty', 'Off Duty'),
        ('resigned', 'Resigned'),
        ('terminated', 'Terminated'),
    ]
    SHIFT_CHOICES = [
        ('morning', 'Morning'),
        ('afternoon', 'Afternoon'),
        ('night', 'Night'),
    ]
    EMPLOYMENT_TYPE_CHOICES = [
        ('full-time', 'Full Time'),
        ('part-time', 'Part Time'),
        ('contract', 'Contract'),
        ('intern', 'Intern'),
    ]

    # Auto ID
    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    employee_id = models.CharField(max_length=20, unique=True, blank=True)

    # Personal Info
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    national_id = models.CharField(max_length=50, blank=True)
    photo = models.URLField(blank=True)

    # Job Info
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, related_name='employees')
    job_role = models.ForeignKey(JobRole, on_delete=models.SET_NULL, null=True, related_name='employees')
    role = models.CharField(max_length=100, blank=True)  # Legacy / display role title
    manager = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='reports'
    )
    shift = models.CharField(max_length=20, choices=SHIFT_CHOICES, default='morning')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    employment_type = models.CharField(max_length=20, choices=EMPLOYMENT_TYPE_CHOICES, default='full-time')
    date_joined = models.DateField(default=timezone.localdate)
    assigned_line = models.CharField(max_length=100, blank=True)

    # Metrics (legacy / computed)
    performance = models.IntegerField(default=100)
    attendance = models.IntegerField(default=100)
    safety_score = models.IntegerField(default=100)

    # Linked system user
    user = models.OneToOneField(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='employee_profile'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.employee_id:
            last = Employee.objects.order_by('-id').first()
            next_id = (last.id + 1) if last else 1
            self.employee_id = f"EMP-{next_id:04d}"
        # Sync role name from job_role
        if self.job_role and not self.role:
            self.role = self.job_role.name
        super().save(*args, **kwargs)

    @property
    def name(self):
        return f"{self.first_name} {self.last_name}"

    def __str__(self):
        return f"{self.employee_id} - {self.name}"

    class Meta:
        ordering = ['first_name', 'last_name']


class EmployeeDocument(models.Model):
    DOC_TYPE_CHOICES = [
        ('id', 'National ID / Passport'),
        ('contract', 'Employment Contract'),
        ('certificate', 'Certificate / Qualification'),
        ('medical', 'Medical Record'),
        ('other', 'Other'),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField(max_length=30, choices=DOC_TYPE_CHOICES)
    name = models.CharField(max_length=200)
    file_url = models.URLField()
    expiry_date = models.DateField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.employee.name} - {self.name}"


# ─── Shift Management ────────────────────────────────────────────────────────

class Shift(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('upcoming', 'Upcoming'),
        ('completed', 'Completed'),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=100)
    shift_type = models.CharField(
        max_length=20,
        choices=[('morning', 'Morning'), ('afternoon', 'Afternoon'), ('night', 'Night')],
        default='morning'
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    capacity = models.IntegerField(default=20)
    supervisor = models.ForeignKey(
        Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='supervised_shifts'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='upcoming')

    def __str__(self):
        return f"{self.name} ({self.start_time} - {self.end_time})"


class ShiftAssignment(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='shift_assignments')
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='assignments')
    date = models.DateField(default=timezone.localdate)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ('employee', 'date')

    def __str__(self):
        return f"{self.employee.name} → {self.shift.name} on {self.date}"


# ─── Attendance & Time Tracking ───────────────────────────────────────────────

class AttendanceRecord(models.Model):
    STATUS_CHOICES = [
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('late', 'Late'),
        ('half-day', 'Half Day'),
        ('on-leave', 'On Leave'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField(default=timezone.localdate)
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='present')
    working_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    overtime_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    corrected_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='corrected_attendance'
    )

    class Meta:
        unique_together = ('employee', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee.name} - {self.date} ({self.status})"

    SHIFT_TIMINGS = {
        'morning': {'start': time(6, 0), 'end': time(14, 0)},
        'afternoon': {'start': time(14, 0), 'end': time(22, 0)},
        'night': {'start': time(22, 0), 'end': time(6, 0)},
    }

    def save(self, *args, **kwargs):
        # Detect if we are setting check_in for the first time
        is_new_clock_in = False
        if self.check_in:
            if not self.pk:
                is_new_clock_in = True
            else:
                # Use a lightweight check to see if check_in was previously null
                was_null = AttendanceRecord.objects.filter(pk=self.pk, check_in__isnull=True).exists()
                if was_null:
                    is_new_clock_in = True

        if is_new_clock_in and not getattr(self, '_admin_correcting', False) and not self.corrected_by:
            # Validate against shift timing
            shift_type = self.employee.shift
            if not shift_type:
                raise ValidationError("Clock-in refused. You do not have a shift assigned to your profile.")
                
            timing = self.SHIFT_TIMINGS.get(shift_type)
            if not timing:
                raise ValidationError(f"Clock-in refused. No timing configuration found for shift type '{shift_type}'.")

            now_time = timezone.localtime(self.check_in).time()
            start = timing['start']
            # Allowed window: exactly from start time to 3 hours after start time
            window_end = (timezone.datetime.combine(timezone.now().date(), start) + timezone.timedelta(hours=3)).time()
            
            is_valid = False
            if start <= window_end:
                is_valid = start <= now_time <= window_end
            else: # Overnight window (e.g. 10 PM to 1 AM)
                is_valid = now_time >= start or now_time <= window_end
            
            if not is_valid:
                raise ValidationError(f"Clock-in refused. You are assigned to the {shift_type} shift ({start.strftime('%H:%M')}). You can only clock in within 3 hours of your shift start time.")

        if self.check_in and self.check_out:
            delta = self.check_out - self.check_in
            total_hours = delta.total_seconds() / 3600
            self.working_hours = round(min(total_hours, 8), 2)
            self.overtime_hours = round(max(total_hours - 8, 0), 2)
        super().save(*args, **kwargs)


# ─── Leave Management ─────────────────────────────────────────────────────────

class LeaveType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True)
    annual_quota = models.IntegerField(default=15)
    carry_forward = models.BooleanField(default=False)
    is_paid = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class LeaveBalance(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_balances')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    year = models.IntegerField(default=timezone.now().year)
    total_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    used_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)

    @property
    def remaining_days(self):
        return self.total_days - self.used_days

    class Meta:
        unique_together = ('employee', 'leave_type', 'year')

    def __str__(self):
        return f"{self.employee.name} - {self.leave_type.name} {self.year}"


class LeaveRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_requests')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    approved_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_leaves'
    )
    rejection_reason = models.TextField(blank=True)
    applied_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)

    @property
    def duration_days(self):
        return (self.end_date - self.start_date).days + 1

    class Meta:
        ordering = ['-applied_on']

    def save(self, *args, **kwargs):
        if not self.pk:  # New request
            # Check against leave balance / quota
            current_year = self.start_date.year
            quota = self.leave_type.annual_quota
            
            existing_leaves = LeaveRequest.objects.filter(
                employee=self.employee,
                leave_type=self.leave_type,
                status__in=['approved', 'pending'],
                start_date__year=current_year
            )
            total_used = sum(r.duration_days for r in existing_leaves)
            
            if total_used + self.duration_days > quota:
                raise ValidationError(f"Leave request denied. {self.leave_type.name} quota is {quota} days per year. You have already used/requested {total_used} days.")

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.employee.name} - {self.leave_type.name} ({self.start_date} to {self.end_date})"


# ─── Training & Certification ─────────────────────────────────────────────────

class Skill(models.Model):
    name = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name


class EmployeeSkill(models.Model):
    LEVEL_CHOICES = [
        ('beginner', 'Beginner'),
        ('intermediate', 'Intermediate'),
        ('expert', 'Expert'),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='skills')
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='beginner')
    certified = models.BooleanField(default=False)
    expiry_date = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ('employee', 'skill')

    def __str__(self):
        return f"{self.employee.name} – {self.skill.name}"


class TrainingProgram(models.Model):
    TYPE_CHOICES = [
        ('safety', 'Safety'),
        ('technical', 'Technical'),
        ('compliance', 'Compliance'),
        ('leadership', 'Leadership'),
    ]
    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('ongoing', 'Ongoing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    name = models.CharField(max_length=200)
    program_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    description = models.TextField(blank=True)
    due_date = models.DateField()
    mandatory = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='planned')
    enrolled_employees = models.ManyToManyField(Employee, related_name='training_programs', blank=True)
    completed_employees = models.ManyToManyField(
        Employee, related_name='completed_training_programs', blank=True
    )

    def __str__(self):
        return self.name


# ─── Safety & Compliance ──────────────────────────────────────────────────────

class SafetyIncident(models.Model):
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('investigating', 'Investigating'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField()
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, related_name='incidents')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='low')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    incident_date = models.DateTimeField()
    reported_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, related_name='reported_incidents'
    )
    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.severity.upper()}] {self.title}"


# ─── Payroll Input ────────────────────────────────────────────────────────────

class PayrollRecord(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payroll_records')
    month = models.IntegerField()
    year = models.IntegerField()
    basic_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    overtime_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    allowances = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_working_hours = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    total_overtime_hours = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    total_leaves = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('employee', 'month', 'year')
        ordering = ['-year', '-month']

    def __str__(self):
        return f"{self.employee.name} - {self.month}/{self.year}"


# ─── Notifications / Announcements ───────────────────────────────────────────

class WorkforceNotification(models.Model):
    TYPE_CHOICES = [
        ('leave', 'Leave'),
        ('shift', 'Shift'),
        ('training', 'Training'),
        ('safety', 'Safety'),
        ('general', 'General'),
        ('payroll', 'Payroll'),
    ]
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='general')
    recipient = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='wf_notifications', null=True, blank=True)
    is_broadcast = models.BooleanField(default=False)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title
