from django.contrib import admin

from .models import (
    QuickBooksConnection,
    QuickBooksEntityLink,
    QuickBooksSyncError,
    QuickBooksSyncRun,
)


@admin.register(QuickBooksConnection)
class QuickBooksConnectionAdmin(admin.ModelAdmin):
    list_display = ("company", "environment", "realm_id", "company_name", "is_active", "last_synced_at")
    list_filter = ("environment", "is_active")
    search_fields = ("company__name", "realm_id", "company_name")


@admin.register(QuickBooksSyncRun)
class QuickBooksSyncRunAdmin(admin.ModelAdmin):
    list_display = ("company", "sync_type", "status", "records_seen", "records_created", "records_updated", "started_at")
    list_filter = ("sync_type", "status")


@admin.register(QuickBooksEntityLink)
class QuickBooksEntityLinkAdmin(admin.ModelAdmin):
    list_display = ("company", "entity_type", "local_object_id", "quickbooks_id", "last_synced_at")
    list_filter = ("entity_type",)


@admin.register(QuickBooksSyncError)
class QuickBooksSyncErrorAdmin(admin.ModelAdmin):
    list_display = ("company", "entity_type", "quickbooks_id", "created_at")
    search_fields = ("message", "quickbooks_id")
