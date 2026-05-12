from rest_framework.routers import DefaultRouter
from .views import QualityCheckViewSet

router = DefaultRouter()
router.register(r'quality-checks', QualityCheckViewSet)

urlpatterns = router.urls
