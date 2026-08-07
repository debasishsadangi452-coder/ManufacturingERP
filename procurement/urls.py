from rest_framework.routers import DefaultRouter
from .views import (
    VendorViewSet,
    VendorPriceListViewSet,
    PurchaseOrderViewSet,
    PurchaseOrderItemViewSet,
    GoodsReceiptViewSet,
    BillViewSet,
    VendorEmailViewSet,
)

router = DefaultRouter()
router.register(r'vendors', VendorViewSet)
router.register(r'vendor-prices', VendorPriceListViewSet)
router.register(r'purchase-orders', PurchaseOrderViewSet)
router.register(r'purchase-order-items', PurchaseOrderItemViewSet)
router.register(r'goods-receipts', GoodsReceiptViewSet)
router.register(r'bills', BillViewSet)
router.register(r'emails', VendorEmailViewSet)

urlpatterns = router.urls
