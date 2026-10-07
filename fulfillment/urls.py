from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AllocateView, DispatchViewSet, FGAllocationViewSet, FGInventoryView, FulfilmentOrderView

router = DefaultRouter()
router.register(r"allocations", FGAllocationViewSet)
router.register(r"dispatches", DispatchViewSet)

urlpatterns = router.urls + [
    path("orders/<int:pk>/", FulfilmentOrderView.as_view(), name="fulfilment-order"),
    path("orders/<int:pk>/allocate/", AllocateView.as_view(), name="fulfilment-allocate"),
    path("fg-inventory/", FGInventoryView.as_view(), name="fg-inventory"),
]
