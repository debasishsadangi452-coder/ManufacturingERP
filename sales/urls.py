from rest_framework.routers import DefaultRouter
from .views import (
    CustomerViewSet,
    SalesOrderViewSet,
    SalesOrderItemViewSet,
    ShipmentViewSet,
)

router = DefaultRouter()

router.register(r'customers', CustomerViewSet)
router.register(r'sales-orders', SalesOrderViewSet)
router.register(r'sales-order-items', SalesOrderItemViewSet)
router.register(r'shipments', ShipmentViewSet)

urlpatterns = router.urls
