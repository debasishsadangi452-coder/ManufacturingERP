from django.urls import path

from . import views

urlpatterns = [
    path("mode/", views.ModeView.as_view()),
    path("vouchers/", views.VoucherListCreateView.as_view()),
    path("vouchers/<int:pk>/detail/", views.VoucherDetailView.as_view()),
    path("vouchers/<int:pk>/<str:action>/", views.VoucherActionView.as_view()),
    path("invoices/<str:kind>/", views.InvoiceView.as_view()),
    path("ledgers/", views.LedgerListView.as_view()),
    path("reports/<str:name>/", views.ReportView.as_view()),
    path("ledgers/<int:pk>/", views.LedgerDetailView.as_view()),
    path("posting-events/", views.PostingEventView.as_view()),
    path("posting-events/<int:pk>/retry/", views.PostingEventView.as_view()),
    path("tax-codes/", views.TaxCodeListView.as_view()),
    path("bill-tax/", views.BillTaxView.as_view()),
    path("settings/", views.SettingsView.as_view()),
    path("allocations/", views.AllocationView.as_view()),
    path("allocations/<int:pk>/<str:action>/", views.AllocationView.as_view()),
    path("notes/", views.NoteView.as_view()),
    path("periods/", views.PeriodListView.as_view()),
    path("periods/<str:kind>/<int:pk>/<str:action>/", views.PeriodActionView.as_view()),
    path("import/<str:kind>/", views.ImportView.as_view()),
    path("import/<str:kind>/<int:pk>/<str:action>/", views.ImportView.as_view()),
    path("stock/", views.StockView.as_view()),
    path("audit/", views.AuditLogView.as_view()),
]
