from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    FiscalYearViewSet,
    AccountingPeriodViewSet,
    AccountTypeViewSet,
    AccountViewSet,
    AccountingSettingsViewSet,
    AccountingFoundationSummaryViewSet,
)

router = DefaultRouter()
router.register(r'fiscal-years', FiscalYearViewSet, basename='accounting-fiscal-years')
router.register(r'periods', AccountingPeriodViewSet, basename='accounting-periods')
router.register(r'account-types', AccountTypeViewSet, basename='accounting-account-types')
router.register(r'accounts', AccountViewSet, basename='accounting-accounts')
router.register(r'settings', AccountingSettingsViewSet, basename='accounting-settings')
router.register(r'summary', AccountingFoundationSummaryViewSet, basename='accounting-summary')

urlpatterns = [
    path('', include(router.urls)),
]
