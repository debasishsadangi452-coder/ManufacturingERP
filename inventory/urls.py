from rest_framework.routers import DefaultRouter
from .views import *

router = DefaultRouter()
router.register("items", ItemViewSet)
router.register("warehouses", WarehouseViewSet)
router.register("stock", StockViewSet)
router.register("batches", BatchViewSet)
router.register("requests", InventoryRequestViewSet, basename="inventory-requests")
router.register("movements", StockMovementViewSet, basename="stock-movements")

urlpatterns = router.urls
