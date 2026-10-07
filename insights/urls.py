from django.urls import path

from .views import AccountingMappingView, ExecutiveDashboardView, KPIDefinitionsView, TraceabilityView

urlpatterns = [
    path("traceability/", TraceabilityView.as_view(), name="traceability-chain"),
    path("executive/", ExecutiveDashboardView.as_view(), name="executive-dashboard"),
    path("kpi-definitions/", KPIDefinitionsView.as_view(), name="kpi-definitions"),
    path("accounting-mapping/", AccountingMappingView.as_view(), name="accounting-mapping"),
]
