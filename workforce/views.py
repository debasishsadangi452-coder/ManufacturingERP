from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db.models import Avg, Sum, Count, Q
from django.core.exceptions import ValidationError
from datetime import date, timedelta

from accounts.permission import IsAdmin, IsHR
from .models import (
    Department, JobRole, Employee, EmployeeDocument,
    Shift, ShiftAssignment,
    AttendanceRecord,
    LeaveType, LeaveBalance, LeaveRequest,
    Skill, EmployeeSkill, TrainingProgram,
    SafetyIncident, PayrollRecord, WorkforceNotification,
)
from .serializers import (
    DepartmentSerializer, JobRoleSerializer,
    EmployeeSerializer, EmployeeListSerializer, EmployeeDocumentSerializer,
    ShiftSerializer, ShiftAssignmentSerializer,
    AttendanceRecordSerializer,
    LeaveTypeSerializer, LeaveBalanceSerializer, LeaveRequestSerializer,
    SkillSerializer, EmployeeSkillSerializer, TrainingProgramSerializer,
    SafetyIncidentSerializer, PayrollRecordSerializer,
    WorkforceNotificationSerializer, WorkforceDashboardSerializer,
)

HR_OR_ADMIN = IsHR | IsAdmin


class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer
    permission_classes = [HR_OR_ADMIN]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'code']


class JobRoleViewSet(viewsets.ModelViewSet):
    queryset = JobRole.objects.select_related('department').all()
    serializer_class = JobRoleSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['department', 'erp_role']


class EmployeeViewSet(viewsets.ModelViewSet):
    queryset = Employee.objects.select_related(
        'department', 'job_role', 'manager', 'user'
    ).all()
    permission_classes = [HR_OR_ADMIN]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'email', 'employee_id', 'role']
    ordering_fields = ['first_name', 'date_joined', 'department__name']
    filterset_fields = ['status', 'shift', 'department', 'employment_type']

    def get_serializer_class(self):
        if self.action == 'list':
            return EmployeeListSerializer
        return EmployeeSerializer

    # ── Clock In / Out ──────────────────────────────────────────────────────

    @action(detail=True, methods=['post'])
    def clock_in(self, request, pk=None):
        employee = self.get_object()
        
        try:
            record, _ = AttendanceRecord.objects.get_or_create(
                employee=employee,
                date=date.today(),
                defaults={'status': 'present'}
            )
            if not record.check_in:
                record.check_in = timezone.now()
                now_hour = timezone.localtime(timezone.now()).hour
                # if clocking in after 9 AM local, mark late
                if now_hour >= 9:
                    record.status = 'late'
                else:
                    record.status = 'present'
                record.save()
            
            employee.status = 'active'
            employee.save()
            return Response({'status': 'Clocked in', 'time': record.check_in})
        except ValidationError as e:
            return Response({'detail': str(e.message) if hasattr(e, 'message') else str(e)}, status=400)

    @action(detail=True, methods=['post'])
    def clock_out(self, request, pk=None):
        employee = self.get_object()
        employee.status = 'off-duty'
        employee.save()

        try:
            record = AttendanceRecord.objects.get(employee=employee, date=date.today())
            if not record.check_out:
                record.check_out = timezone.now()
                record.save()
            return Response({'status': 'Clocked out', 'working_hours': record.working_hours})
        except AttendanceRecord.DoesNotExist:
            return Response({'detail': 'No check-in found for today.'}, status=400)

    # ── Reports ─────────────────────────────────────────────────────────────

    @action(detail=False, methods=['get'])
    def by_department(self, request):
        data = (
            Employee.objects.filter(status__in=['active', 'training', 'on-leave'])
            .values('department__name')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        return Response(list(data))

    @action(detail=False, methods=['get'])
    def availability(self, request):
        """Real-time workforce availability per shift."""
        shifts = Shift.objects.all()
        result = []
        for sh in shifts:
            employees_in_shift = Employee.objects.filter(shift=sh.shift_type, status='active')
            result.append({
                'shift': sh.name,
                'shift_type': sh.shift_type,
                'available': employees_in_shift.count(),
                'capacity': sh.capacity,
                'shortage': max(sh.capacity - employees_in_shift.count(), 0),
            })
        return Response(result)

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        employee = self.get_object()
        reason = request.data.get('reason', 'resigned')
        employee.status = reason if reason in ['resigned', 'terminated'] else 'resigned'
        employee.save()
        return Response({'status': 'Employee deactivated'})

    @action(detail=True, methods=['post'])
    def assign_shift(self, request, pk=None):
        employee = self.get_object()
        shift_id = request.data.get('shift_id')
        shift_date = request.data.get('date', str(date.today()))
        try:
            shift = Shift.objects.get(id=shift_id)
        except Shift.DoesNotExist:
            return Response({'detail': 'Shift not found'}, status=404)

        assignment, created = ShiftAssignment.objects.update_or_create(
            employee=employee, date=shift_date,
            defaults={'shift': shift, 'notes': request.data.get('notes', '')}
        )
        return Response(ShiftAssignmentSerializer(assignment).data)


class EmployeeDocumentViewSet(viewsets.ModelViewSet):
    queryset = EmployeeDocument.objects.all()
    serializer_class = EmployeeDocumentSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['employee', 'doc_type']


class ShiftViewSet(viewsets.ModelViewSet):
    queryset = Shift.objects.select_related('supervisor').all()
    serializer_class = ShiftSerializer
    permission_classes = [HR_OR_ADMIN]


class ShiftAssignmentViewSet(viewsets.ModelViewSet):
    queryset = ShiftAssignment.objects.select_related('employee', 'shift').all()
    serializer_class = ShiftAssignmentSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['employee', 'shift', 'date']

    @action(detail=False, methods=['get'])
    def weekly_roster(self, request):
        """Return 7-day roster starting from a given date."""
        start_date = request.query_params.get('start_date', str(date.today()))
        end_date = (date.fromisoformat(start_date) + timedelta(days=6)).isoformat()
        assignments = ShiftAssignment.objects.filter(
            date__range=[start_date, end_date]
        ).select_related('employee', 'shift')
        return Response(ShiftAssignmentSerializer(assignments, many=True).data)


class AttendanceViewSet(viewsets.ModelViewSet):
    queryset = AttendanceRecord.objects.select_related('employee').all()
    serializer_class = AttendanceRecordSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['employee', 'date', 'status']
    ordering_fields = ['date']

    @action(detail=False, methods=['get'])
    def today(self, request):
        records = AttendanceRecord.objects.filter(date=date.today()).select_related('employee')
        return Response(AttendanceRecordSerializer(records, many=True).data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Monthly attendance summary."""
        month = int(request.query_params.get('month', date.today().month))
        year = int(request.query_params.get('year', date.today().year))
        records = AttendanceRecord.objects.filter(date__month=month, date__year=year)
        total = records.count()
        present = records.filter(status__in=['present', 'late']).count()
        absent = records.filter(status='absent').count()
        late = records.filter(status='late').count()
        avg_hours = records.aggregate(avg=Avg('working_hours'))['avg'] or 0
        total_overtime = records.aggregate(total=Sum('overtime_hours'))['total'] or 0
        return Response({
            'month': month, 'year': year,
            'total_records': total,
            'present': present,
            'absent': absent,
            'late': late,
            'avg_working_hours': round(float(avg_hours), 2),
            'total_overtime_hours': round(float(total_overtime), 2),
        })

    def perform_create(self, serializer):
        # Allow HR/Admin to manually create records at any time
        instance = serializer.save(corrected_by=self.request.user)
        instance._admin_correcting = True
        instance.save()

    def perform_update(self, serializer):
        # Allow HR/Admin to manually update records at any time
        instance = serializer.save(corrected_by=self.request.user)
        instance._admin_correcting = True
        instance.save()

    @action(detail=True, methods=['patch'])
    def correct(self, request, pk=None):
        """HR manual correction."""
        record = self.get_object()
        record._admin_correcting = True
        serializer = self.get_serializer(record, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        record = serializer.save(corrected_by=request.user)
        return Response(serializer.data)


class LeaveTypeViewSet(viewsets.ModelViewSet):
    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer
    permission_classes = [IsAuthenticated]


class LeaveBalanceViewSet(viewsets.ModelViewSet):
    queryset = LeaveBalance.objects.select_related('employee', 'leave_type').all()
    serializer_class = LeaveBalanceSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['employee', 'leave_type', 'year']


class LeaveRequestViewSet(viewsets.ModelViewSet):
    queryset = LeaveRequest.objects.select_related('employee', 'leave_type', 'approved_by').all()
    serializer_class = LeaveRequestSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['status', 'leave_type', 'employee']

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        leave = self.get_object()
        if leave.status != 'pending':
            return Response({'detail': 'Only pending requests can be approved.'}, status=400)
        leave.status = 'approved'
        leave.approved_by = request.user
        leave.save()

        # Update leave balance
        current_year = date.today().year
        balance, _ = LeaveBalance.objects.get_or_create(
            employee=leave.employee, leave_type=leave.leave_type, year=current_year
        )
        balance.used_days += leave.duration_days
        balance.save()

        # Update employee status
        today = date.today()
        if leave.start_date <= today <= leave.end_date:
            leave.employee.status = 'on-leave'
            leave.employee.save()

        # Notify
        WorkforceNotification.objects.create(
            title=f"Leave Approved",
            message=f"Your {leave.leave_type.name} request from {leave.start_date} to {leave.end_date} has been approved.",
            notification_type='leave',
            recipient=leave.employee,
        )
        return Response(LeaveRequestSerializer(leave).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        leave = self.get_object()
        if leave.status != 'pending':
            return Response({'detail': 'Only pending requests can be rejected.'}, status=400)
        leave.status = 'rejected'
        leave.approved_by = request.user
        leave.rejection_reason = request.data.get('reason', '')
        leave.save()
        WorkforceNotification.objects.create(
            title="Leave Rejected",
            message=f"Your {leave.leave_type.name} request was rejected. Reason: {leave.rejection_reason}",
            notification_type='leave',
            recipient=leave.employee,
        )
        return Response(LeaveRequestSerializer(leave).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        leave = self.get_object()
        leave.status = 'cancelled'
        leave.save()
        return Response({'status': 'Cancelled'})

    @action(detail=False, methods=['get'])
    def calendar(self, request):
        """All approved leaves for the calendar view."""
        month = int(request.query_params.get('month', date.today().month))
        year = int(request.query_params.get('year', date.today().year))
        leaves = LeaveRequest.objects.filter(
            status='approved',
            start_date__year=year, start_date__month=month
        ).select_related('employee', 'leave_type')
        return Response(LeaveRequestSerializer(leaves, many=True).data)


class SkillViewSet(viewsets.ModelViewSet):
    queryset = Skill.objects.all()
    serializer_class = SkillSerializer
    permission_classes = [HR_OR_ADMIN]


class EmployeeSkillViewSet(viewsets.ModelViewSet):
    queryset = EmployeeSkill.objects.select_related('employee', 'skill').all()
    serializer_class = EmployeeSkillSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['employee', 'skill', 'certified']

    @action(detail=False, methods=['get'])
    def expiring_soon(self, request):
        """Certifications expiring within 30 days."""
        threshold = date.today() + timedelta(days=30)
        expiring = EmployeeSkill.objects.filter(
            certified=True,
            expiry_date__lte=threshold,
            expiry_date__gte=date.today()
        ).select_related('employee', 'skill')
        return Response(EmployeeSkillSerializer(expiring, many=True).data)


class TrainingProgramViewSet(viewsets.ModelViewSet):
    queryset = TrainingProgram.objects.all()
    serializer_class = TrainingProgramSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['program_type', 'mandatory', 'status']

    @action(detail=True, methods=['post'])
    def enroll(self, request, pk=None):
        program = self.get_object()
        employee_ids = request.data.get('employee_ids', [])
        employees = Employee.objects.filter(id__in=employee_ids)
        program.enrolled_employees.add(*employees)
        return Response({'enrolled': [e.name for e in employees]})

    @action(detail=True, methods=['post'])
    def mark_completed(self, request, pk=None):
        program = self.get_object()
        employee_ids = request.data.get('employee_ids', [])
        employees = Employee.objects.filter(id__in=employee_ids)
        program.completed_employees.add(*employees)
        return Response({'completed': [e.name for e in employees]})


class SafetyIncidentViewSet(viewsets.ModelViewSet):
    queryset = SafetyIncident.objects.select_related(
        'employee', 'department', 'reported_by'
    ).all()
    serializer_class = SafetyIncidentSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['severity', 'status', 'department']

    def perform_create(self, serializer):
        serializer.save(reported_by=self.request.user)

    @action(detail=True, methods=['post'])
    def resolve(self, request, pk=None):
        incident = self.get_object()
        incident.status = 'resolved'
        incident.resolution_notes = request.data.get('notes', '')
        incident.save()
        return Response(SafetyIncidentSerializer(incident).data)


class PayrollRecordViewSet(viewsets.ModelViewSet):
    queryset = PayrollRecord.objects.select_related('employee').all()
    serializer_class = PayrollRecordSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['employee', 'month', 'year']

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Auto-generate payroll inputs for a month from attendance data."""
        month = int(request.data.get('month', date.today().month))
        year = int(request.data.get('year', date.today().year))
        employees = Employee.objects.filter(status__in=['active', 'on-leave'])
        created_records = []

        for employee in employees:
            records = AttendanceRecord.objects.filter(
                employee=employee, date__month=month, date__year=year
            )
            total_hours = float(records.aggregate(Sum('working_hours'))['working_hours__sum'] or 0)
            overtime_hours = float(records.aggregate(Sum('overtime_hours'))['overtime_hours__sum'] or 0)
            leaves = records.filter(status='on-leave').count()

            pr, created = PayrollRecord.objects.update_or_create(
                employee=employee, month=month, year=year,
                defaults={
                    'total_working_hours': total_hours,
                    'total_overtime_hours': overtime_hours,
                    'total_leaves': leaves,
                    'overtime_pay': overtime_hours * 150,  # default overtime rate
                }
            )
            created_records.append(pr.id)

        return Response({'generated': len(created_records), 'month': month, 'year': year})

    @action(detail=False, methods=['get'])
    def export(self, request):
        """Payroll-ready report."""
        month = int(request.query_params.get('month', date.today().month))
        year = int(request.query_params.get('year', date.today().year))
        records = PayrollRecord.objects.filter(month=month, year=year).select_related('employee')
        return Response(PayrollRecordSerializer(records, many=True).data)


class WorkforceNotificationViewSet(viewsets.ModelViewSet):
    queryset = WorkforceNotification.objects.all()
    serializer_class = WorkforceNotificationSerializer
    permission_classes = [HR_OR_ADMIN]
    filterset_fields = ['notification_type', 'is_read', 'recipient']

    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.save()
        return Response({'status': 'Marked as read'})

    @action(detail=False, methods=['post'])
    def broadcast(self, request):
        title = request.data.get('title', '')
        message = request.data.get('message', '')
        notif_type = request.data.get('type', 'general')
        employees = Employee.objects.filter(status__in=['active', 'on-leave', 'training'])
        notifications = [
            WorkforceNotification(
                title=title, message=message,
                notification_type=notif_type, recipient=emp, is_broadcast=True
            ) for emp in employees
        ]
        WorkforceNotification.objects.bulk_create(notifications)
        return Response({'sent': len(notifications)})


class WorkforceDashboardView(APIView):
    permission_classes = [HR_OR_ADMIN]

    def get(self, request):
        today = date.today()
        employees = Employee.objects.all()
        active = employees.filter(status='active').count()
        on_leave = employees.filter(status='on-leave').count()
        in_training = employees.filter(status='training').count()
        resigned = employees.filter(status__in=['resigned', 'terminated']).count()
        total = employees.count()

        # Attendance rate (last 30 days)
        last_30 = today - timedelta(days=30)
        att_records = AttendanceRecord.objects.filter(date__gte=last_30)
        total_records = att_records.count()
        present_records = att_records.filter(status__in=['present', 'late']).count()
        att_rate = round((present_records / max(total_records, 1)) * 100, 1)

        # Overtime this month
        overtime_total = att_records.filter(
            date__month=today.month
        ).aggregate(Sum('overtime_hours'))['overtime_hours__sum'] or 0

        # Department breakdown
        dept_breakdown = list(
            employees.values('department__name')
            .annotate(count=Count('id'), active=Count('id', filter=Q(status='active')))
            .order_by('-count')
        )

        # Shift summary
        shifts = Shift.objects.all()
        shift_summary = []
        for sh in shifts:
            cnt = employees.filter(shift=sh.shift_type, status='active').count()
            shift_summary.append({
                'name': sh.name,
                'type': sh.shift_type,
                'active_employees': cnt,
                'capacity': sh.capacity,
            })

        return Response({
            'total_employees': total,
            'active_today': active,
            'on_leave': on_leave,
            'in_training': in_training,
            'resigned': resigned,
            'pending_leave_requests': LeaveRequest.objects.filter(status='pending').count(),
            'open_incidents': SafetyIncident.objects.filter(status__in=['open', 'investigating']).count(),
            'upcoming_trainings': TrainingProgram.objects.filter(
                status__in=['planned', 'ongoing'], due_date__gte=today
            ).count(),
            'department_breakdown': dept_breakdown,
            'shift_summary': shift_summary,
            'attendance_rate': att_rate,
            'overtime_hours_this_month': float(overtime_total),
        })


# ── Employee Self-Service (limited access) ─────────────────────────────────

class MyProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            employee = Employee.objects.select_related(
                'department', 'job_role', 'manager'
            ).get(user=request.user)
            return Response(EmployeeSerializer(employee).data)
        except Employee.DoesNotExist:
            return Response({'detail': 'No employee profile linked.'}, status=404)


class MyAttendanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            employee = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return Response({'detail': 'No employee profile linked.'}, status=404)
        month = int(request.query_params.get('month', date.today().month))
        year = int(request.query_params.get('year', date.today().year))
        records = AttendanceRecord.objects.filter(
            employee=employee, date__month=month, date__year=year
        )
        return Response(AttendanceRecordSerializer(records, many=True).data)

    def post(self, request):
        """Self clock-in/out"""
        try:
            employee = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return Response({'detail': 'No employee profile linked.'}, status=404)
        
        action_type = request.data.get('action')
        try:
            if action_type == 'clock_in':
                record, _ = AttendanceRecord.objects.get_or_create(
                    employee=employee, date=date.today(), defaults={'status': 'present'}
                )
                if not record.check_in:
                    record.check_in = timezone.now()
                    record.status = 'late' if timezone.localtime(timezone.now()).hour >= 9 else 'present'
                    record.save()
                    employee.status = 'active'
                    employee.save()
                return Response({'status': 'Clocked in', 'time': record.check_in})
            elif action_type == 'clock_out':
                try:
                    record = AttendanceRecord.objects.get(employee=employee, date=date.today())
                    if not record.check_out:
                        record.check_out = timezone.now()
                        record.save()
                        employee.status = 'off-duty'
                        employee.save()
                    return Response({'status': 'Clocked out', 'working_hours': record.working_hours})
                except AttendanceRecord.DoesNotExist:
                    return Response({'detail': 'No check-in found.'}, status=400)
            return Response({'detail': 'Invalid action.'}, status=400)
        except ValidationError as e:
            msg = e.message if hasattr(e, 'message') else str(e)
            return Response({'detail': msg}, status=400)


class MyLeaveView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            employee = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return Response({'detail': 'No employee profile linked.'}, status=404)
        requests = LeaveRequest.objects.filter(employee=employee).select_related('leave_type')
        balances = LeaveBalance.objects.filter(
            employee=employee, year=date.today().year
        ).select_related('leave_type')
        return Response({
            'requests': LeaveRequestSerializer(requests, many=True).data,
            'balances': LeaveBalanceSerializer(balances, many=True).data,
        })

    def post(self, request):
        try:
            employee = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return Response({'detail': 'No employee profile linked.'}, status=404)
        data = request.data.copy()
        data['employee'] = employee.id
        serializer = LeaveRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        try:
            leave = serializer.save()
            # Notify supervisors
            if employee.manager:
                WorkforceNotification.objects.create(
                    title=f"New Leave Request from {employee.name}",
                    message=f"{employee.name} has applied for {leave.leave_type.name} from {leave.start_date} to {leave.end_date}.",
                    notification_type='leave',
                    recipient=employee.manager,
                )
            return Response(serializer.data, status=201)
        except ValidationError as e:
            msg = e.message if hasattr(e, 'message') else str(e)
            return Response({'detail': msg}, status=400)


class MyShiftView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            employee = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return Response({'detail': 'No employee profile linked.'}, status=404)
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        assignments = ShiftAssignment.objects.filter(
            employee=employee, date__range=[week_start, week_end]
        ).select_related('shift')
        return Response({
            'current_shift': employee.shift,
            'weekly_assignments': ShiftAssignmentSerializer(assignments, many=True).data,
        })


class MyNotificationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            employee = Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return Response([])
        notifications = WorkforceNotification.objects.filter(
            Q(recipient=employee) | Q(is_broadcast=True)
        ).order_by('-created_at')[:20]
        return Response(WorkforceNotificationSerializer(notifications, many=True).data)
