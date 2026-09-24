from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    FiscalYearViewSet,
    AccountingPeriodViewSet,
    AccountTypeViewSet,
    AccountViewSet,
    AccountingSettingsViewSet,
    AccountingFoundationSummaryViewSet,
    ERPContextViewSet,
    JournalEntryViewSet,
    GeneralLedgerViewSet,
    AccountsReceivableViewSet,
    AccountsPayableViewSet,
    SalesAccountingViewSet,
)

router = DefaultRouter()
router.register(r'fiscal-years', FiscalYearViewSet, basename='accounting-fiscal-years')
router.register(r'periods', AccountingPeriodViewSet, basename='accounting-periods')
router.register(r'account-types', AccountTypeViewSet, basename='accounting-account-types')
router.register(r'accounts', AccountViewSet, basename='accounting-accounts')
router.register(r'settings', AccountingSettingsViewSet, basename='accounting-settings')
router.register(r'summary', AccountingFoundationSummaryViewSet, basename='accounting-summary')
router.register(r'erp-context', ERPContextViewSet, basename='accounting-erp-context')
router.register(r'journal-entries', JournalEntryViewSet, basename='accounting-journal-entries')
router.register(r'general-ledger', GeneralLedgerViewSet, basename='accounting-general-ledger')
router.register(r'receivables', AccountsReceivableViewSet, basename='accounting-receivables')
router.register(r'payables', AccountsPayableViewSet, basename='accounting-payables')
router.register(r'sales', SalesAccountingViewSet, basename='accounting-sales')

urlpatterns = [
    path('', include(router.urls)),
]
