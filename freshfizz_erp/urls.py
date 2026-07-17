from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from rest_framework_simplejwt.views import TokenRefreshView
from accounts.views import EmailOrUsernameTokenObtainPairView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

def health(request):
    return JsonResponse({"status": "ok"})

urlpatterns = [
    path('api/health/', health, name='health'),
    path('admin/', admin.site.urls),

    # 🔐 Auth
    path('api/auth/', include('accounts.urls')),
    path('api/token/', EmailOrUsernameTokenObtainPairView.as_view(), name='token'),
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
    path('api/quickbooks/', include('quickbooks.urls')),

    # ⭐ REQUIRED FOR SWAGGER
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),

    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui'
    ),
]
