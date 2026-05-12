from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, AuditLogViewSet, live_stream

router = DefaultRouter()
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'audit-logs', AuditLogViewSet)

urlpatterns = router.urls + [
    path('stream/', live_stream, name='live-stream'),
]
