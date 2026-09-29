from django.urls import path

from . import views

urlpatterns = [
    path("connect/", views.connect, name="quickbooks-connect"),
    path("callback/", views.callback, name="quickbooks-callback"),
    path("status/", views.status_view, name="quickbooks-status"),
    path("disconnect/", views.disconnect, name="quickbooks-disconnect"),
    path("sync/", views.sync, name="quickbooks-sync"),
    path("push/", views.push, name="quickbooks-push"),
    path("push-all/", views.push_all_view, name="quickbooks-push-all"),
    path("overview/", views.overview, name="quickbooks-overview"),
    path("accounts/", views.quickbooks_accounts, name="quickbooks-accounts"),
    path("account-mappings/", views.account_mappings, name="quickbooks-account-mappings"),
    path("account-mappings/auto/", views.auto_map_accounts, name="quickbooks-auto-map"),
    path("retry-errors/", views.retry_errors, name="quickbooks-retry-errors"),
]
