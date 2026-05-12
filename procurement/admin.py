from django.contrib import admin
from .models import Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "category", "email", "phone", "rating"]
    search_fields = ["name", "category"]


@admin.register(VendorPriceList)
class VendorPriceListAdmin(admin.ModelAdmin):
    list_display = ["vendor", "item", "unit_price", "currency", "min_order_qty", "lead_time_days", "is_active", "effective_date"]
    list_filter = ["vendor", "is_active", "currency"]
    search_fields = ["vendor__name", "item__name"]


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ["id", "vendor", "status", "priority", "total_amount", "expected_delivery", "created_at"]
    list_filter = ["status", "priority"]


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = ["id", "purchase_order", "item", "quantity", "unit_price"]


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(admin.ModelAdmin):
    list_display = ["id", "purchase_order", "warehouse", "received_at"]