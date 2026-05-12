from django.contrib import admin
from .models import QualityCheck


@admin.register(QualityCheck)
class QualityCheckAdmin(admin.ModelAdmin):
    list_display = (
        "production_order",
        "status",
        "inspected_at",
    )
    list_filter = ("status", "inspected_at")
    search_fields = ("production_order__id",)
    readonly_fields = ("inspected_at",)