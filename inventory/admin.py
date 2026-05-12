from django.contrib import admin
from .models import Item, Warehouse, Stock, Batch, StockMovement


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "is_finished_good")
    search_fields = ("name", "category")
    list_filter = ("category",)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("name", "location")
    search_fields = ("name", "location")


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("item", "warehouse", "quantity")
    list_filter = ("warehouse",)
    search_fields = ("item__name",)


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("item", "batch_number", "expiry_date", "quantity")
    list_filter = ("expiry_date",)
    search_fields = ("item__name", "batch_number")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "item",
        "warehouse",
        "movement_type",
        "quantity",
        "created_by",
        "created_at",
    )
    list_filter = ("movement_type", "warehouse")
    search_fields = ("item__name", "reference")
    readonly_fields = ("created_at",)