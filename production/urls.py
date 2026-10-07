from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    CapacityPlanView,
    ManufacturingSettingsView,
    ProductionLineViewSet,
    ProductionOperationViewSet,
    ProductionOrderViewSet,
    ProjectionDashboardAPIView,
    RecipeIngredientViewSet,
    RecipeViewSet,
    ResourceUnavailabilityViewSet,
    ResourceViewSet,
    RoutingStepViewSet,
    ScheduleAllView,
    ScrapRecordViewSet,
    WIPBoardView,
)

router = DefaultRouter()

router.register(r'recipes', RecipeViewSet)
router.register(r'recipe-ingredients', RecipeIngredientViewSet)
router.register(r'production-orders', ProductionOrderViewSet)
router.register(r'lines', ProductionLineViewSet)
# Track B
router.register(r'operations', ProductionOperationViewSet)
router.register(r'resources', ResourceViewSet)
router.register(r'resource-unavailability', ResourceUnavailabilityViewSet)
router.register(r'routing-steps', RoutingStepViewSet)
router.register(r'scrap', ScrapRecordViewSet)

urlpatterns = router.urls
urlpatterns += [
    path("projection/", ProjectionDashboardAPIView.as_view(), name="projection-dashboard"),
    path("wip/", WIPBoardView.as_view(), name="wip-board"),
    path("capacity/", CapacityPlanView.as_view(), name="capacity-plan"),
    path("schedule/", ScheduleAllView.as_view(), name="schedule-all"),
    path("settings/", ManufacturingSettingsView.as_view(), name="manufacturing-settings"),
]
