from rest_framework.routers import DefaultRouter
from .views import (
    CustomerViewSet,
    CustomerPaymentViewSet,
    DiscountTypeViewSet,
    SummaryInvoiceViewSet,
    InboundOrderEmailViewSet,
    InvoiceViewSet,
    PriceListViewSet,
    QuotationViewSet,
    SalesOrderViewSet,
    SalesOrderItemViewSet,
    ShipmentViewSet,
)

router = DefaultRouter()

router.register(r'customers', CustomerViewSet)
router.register(r"quotations", QuotationViewSet)
router.register(r'sales-orders', SalesOrderViewSet)
router.register(r'sales-order-items', SalesOrderItemViewSet)
router.register(r'shipments', ShipmentViewSet)
router.register(r'invoices', InvoiceViewSet)
router.register(r'discount-types', DiscountTypeViewSet)
router.register(r'summary-invoices', SummaryInvoiceViewSet)
router.register(r'payments', CustomerPaymentViewSet)
router.register(r'inbound-orders', InboundOrderEmailViewSet)
router.register(r'price-list', PriceListViewSet, basename='sales-price-list')

urlpatterns = router.urls
