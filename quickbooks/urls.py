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
]
