from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    ProjectionDashboardAPIView,
    RecipeViewSet,
    RecipeIngredientViewSet,
    ProductionOrderViewSet,
    ProductionLineViewSet,
)

router = DefaultRouter()

router.register(r'recipes', RecipeViewSet)
router.register(r'recipe-ingredients', RecipeIngredientViewSet)
router.register(r'production-orders', ProductionOrderViewSet)
router.register(r'lines', ProductionLineViewSet)

urlpatterns = router.urls
urlpatterns += [
    path("projection/", ProjectionDashboardAPIView.as_view(), name="projection-dashboard"),
]
