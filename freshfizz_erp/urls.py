from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),

    # 🔐 Auth
    path('api/auth/', include('accounts.urls')),
    path('api/token/', TokenObtainPairView.as_view(), name='token'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # 📦 ERP Modules
    path('api/inventory/', include('inventory.urls')),
    path('api/procurement/', include('procurement.urls')),
    path('api/production/', include('production.urls')),
    path('api/quality/', include('quality.urls')),
    path('api/sales/', include('sales.urls')),
    path('api/maintenance/', include('maintenance.urls')),
    path('api/logistics/', include('logistics.urls')),
    path('api/workforce/', include('workforce.urls')),
    path('api/core/', include('core.urls')),
    path('api/finance/', include('finance.urls')),
    path('api/ai/', include('ai_assistant.urls')),

    # ⭐ REQUIRED FOR SWAGGER
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),

    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui'
    ),
]
