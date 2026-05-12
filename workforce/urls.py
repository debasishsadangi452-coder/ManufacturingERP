from rest_framework.routers import DefaultRouter
from django.urls import path
from .views import (
    DepartmentViewSet, JobRoleViewSet,
    EmployeeViewSet, EmployeeDocumentViewSet,
    ShiftViewSet, ShiftAssignmentViewSet,
    AttendanceViewSet,
    LeaveTypeViewSet, LeaveBalanceViewSet, LeaveRequestViewSet,
    SkillViewSet, EmployeeSkillViewSet, TrainingProgramViewSet,
    SafetyIncidentViewSet, PayrollRecordViewSet,
    WorkforceNotificationViewSet,
    WorkforceDashboardView,
    MyProfileView, MyAttendanceView, MyLeaveView, MyShiftView, MyNotificationsView,
)

router = DefaultRouter()
router.register(r'departments', DepartmentViewSet)
router.register(r'job-roles', JobRoleViewSet)
router.register(r'employees', EmployeeViewSet)
router.register(r'employee-documents', EmployeeDocumentViewSet)
router.register(r'shifts', ShiftViewSet)
router.register(r'shift-assignments', ShiftAssignmentViewSet)
router.register(r'attendance', AttendanceViewSet)
router.register(r'leave-types', LeaveTypeViewSet)
router.register(r'leave-balances', LeaveBalanceViewSet)
router.register(r'leave-requests', LeaveRequestViewSet)
router.register(r'skills', SkillViewSet)
router.register(r'employee-skills', EmployeeSkillViewSet)
router.register(r'training', TrainingProgramViewSet)
router.register(r'safety-incidents', SafetyIncidentViewSet)
router.register(r'payroll', PayrollRecordViewSet)
router.register(r'notifications', WorkforceNotificationViewSet)

urlpatterns = router.urls + [
    # Dashboard
    path('dashboard/', WorkforceDashboardView.as_view(), name='workforce-dashboard'),

    # Self-service (employee-facing)
    path('me/profile/', MyProfileView.as_view(), name='my-profile'),
    path('me/attendance/', MyAttendanceView.as_view(), name='my-attendance'),
    path('me/leave/', MyLeaveView.as_view(), name='my-leave'),
    path('me/shift/', MyShiftView.as_view(), name='my-shift'),
    path('me/notifications/', MyNotificationsView.as_view(), name='my-notifications'),
]
