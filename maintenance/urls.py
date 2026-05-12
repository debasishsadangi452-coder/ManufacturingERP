from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EquipmentViewSet, MaintenanceTaskViewSet

router = DefaultRouter()
router.register(r'equipment', EquipmentViewSet)
router.register(r'tasks', MaintenanceTaskViewSet)

urlpatterns = router.urls
