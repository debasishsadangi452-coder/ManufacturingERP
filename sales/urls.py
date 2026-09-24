from rest_framework.routers import DefaultRouter
from .views import (
    CustomerViewSet,
    CustomerPaymentViewSet,
    InboundOrderEmailViewSet,
    InvoiceViewSet,
    PriceListViewSet,
    SalesOrderViewSet,
    SalesOrderItemViewSet,
    ShipmentViewSet,
)

router = DefaultRouter()

router.register(r'customers', CustomerViewSet)
router.register(r'sales-orders', SalesOrderViewSet)
router.register(r'sales-order-items', SalesOrderItemViewSet)
router.register(r'shipments', ShipmentViewSet)
router.register(r'invoices', InvoiceViewSet)
router.register(r'payments', CustomerPaymentViewSet)
router.register(r'inbound-orders', InboundOrderEmailViewSet)
router.register(r'price-list', PriceListViewSet, basename='sales-price-list')

urlpatterns = router.urls
