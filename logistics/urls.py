from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DriverViewSet, VehicleViewSet, DeliveryRouteViewSet, ShipmentViewSet

router = DefaultRouter()
router.register(r'drivers', DriverViewSet)
router.register(r'vehicles', VehicleViewSet)
router.register(r'routes', DeliveryRouteViewSet)
router.register(r'shipments', ShipmentViewSet)

urlpatterns = router.urls
