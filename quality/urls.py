from rest_framework.routers import DefaultRouter
from .views import QualityCheckViewSet, IncomingQualityCheckViewSet

router = DefaultRouter()
router.register(r'quality-checks', QualityCheckViewSet)
router.register(r'incoming', IncomingQualityCheckViewSet, basename='incoming-quality')

urlpatterns = router.urls
