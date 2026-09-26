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
    PurchaseAccountingViewSet,
    InventoryAccountingViewSet,
    ManufacturingAccountingViewSet,
    ExpenseCategoryViewSet,
    ExpenseViewSet,
    BankAccountViewSet,
    BankTransactionViewSet,
    BankReconciliationViewSet,
    BankingSummaryViewSet,
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
router.register(r'purchases', PurchaseAccountingViewSet, basename='accounting-purchases')
router.register(r'inventory', InventoryAccountingViewSet, basename='accounting-inventory')
router.register(r'manufacturing', ManufacturingAccountingViewSet, basename='accounting-manufacturing')
router.register(r'expenses', ExpenseViewSet, basename='accounting-expenses')
router.register(r'expense-categories', ExpenseCategoryViewSet, basename='accounting-expense-categories')
router.register(r'bank-accounts', BankAccountViewSet, basename='accounting-bank-accounts')
router.register(r'bank-transactions', BankTransactionViewSet, basename='accounting-bank-transactions')
router.register(r'bank-reconciliations', BankReconciliationViewSet, basename='accounting-bank-reconciliations')
router.register(r'banking', BankingSummaryViewSet, basename='accounting-banking')

urlpatterns = [
    path('', include(router.urls)),
]

