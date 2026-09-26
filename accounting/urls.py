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
    PaymentViewSet,
    PaymentAllocationViewSet,
    TaxCodeViewSet,
    TaxTransactionLineViewSet,
    TaxAdjustmentViewSet,
    TaxManagementViewSet,
    FinancialReportsViewSet,
    AccountingDashboardViewSet,
    DatabaseArchitectureViewSet,
    APIArchitectureViewSet,
    UIArchitectureViewSet,
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
router.register(r'payments', PaymentViewSet, basename='accounting-payments')
router.register(r'payment-allocations', PaymentAllocationViewSet, basename='accounting-payment-allocations')
router.register(r'tax-codes', TaxCodeViewSet, basename='accounting-tax-codes')
router.register(r'tax-lines', TaxTransactionLineViewSet, basename='accounting-tax-lines')
router.register(r'tax-adjustments', TaxAdjustmentViewSet, basename='accounting-tax-adjustments')
router.register(r'taxes', TaxManagementViewSet, basename='accounting-taxes')
router.register(r'reports', FinancialReportsViewSet, basename='accounting-reports')
router.register(r'dashboard', AccountingDashboardViewSet, basename='accounting-dashboard')
router.register(r'database-architecture', DatabaseArchitectureViewSet, basename='accounting-database-architecture')
router.register(r'api-architecture', APIArchitectureViewSet, basename='accounting-api-architecture')
router.register(r'ui-architecture', UIArchitectureViewSet, basename='accounting-ui-architecture')



urlpatterns = [
    path('', include(router.urls)),
]


