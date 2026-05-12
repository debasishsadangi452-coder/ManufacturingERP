from rest_framework.routers import DefaultRouter
from .views import (
    VendorViewSet,
    VendorPriceListViewSet,
    PurchaseOrderViewSet,
    PurchaseOrderItemViewSet,
    GoodsReceiptViewSet,
)

router = DefaultRouter()
router.register(r'vendors', VendorViewSet)
router.register(r'vendor-prices', VendorPriceListViewSet)
router.register(r'purchase-orders', PurchaseOrderViewSet)
router.register(r'purchase-order-items', PurchaseOrderItemViewSet)
router.register(r'goods-receipts', GoodsReceiptViewSet)

urlpatterns = router.urls
