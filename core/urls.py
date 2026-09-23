from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, AuditLogViewSet, poll_changes, contact_request

router = DefaultRouter()
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'audit-logs', AuditLogViewSet)

urlpatterns = router.urls + [
    path('changes/', poll_changes, name='poll-changes'),
    path('contact/', contact_request, name='contact-request'),
]
