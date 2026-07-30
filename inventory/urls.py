from rest_framework.routers import DefaultRouter
from .views import *

router = DefaultRouter()
router.register("units", UnitOfMeasureViewSet, basename="units")
router.register("transfers", StockTransferViewSet, basename="transfers")
router.register("cycle-counts", CycleCountViewSet, basename="cycle-counts")
router.register("items", ItemViewSet)
router.register("warehouses", WarehouseViewSet)
router.register("stock", StockViewSet)
router.register("batches", BatchViewSet)
router.register("requests", InventoryRequestViewSet, basename="inventory-requests")
router.register("movements", StockMovementViewSet, basename="stock-movements")
router.register("onboarding", QuickBooksOnboardingViewSet, basename="qb-onboarding")
router.register("bom", BOMViewSet, basename="bom")

urlpatterns = router.urls
