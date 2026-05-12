from django.contrib import admin
from .models import Customer, SalesOrder, SalesOrderItem, Shipment


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone")
    search_fields = ("name", "email", "phone")


class SalesOrderItemInline(admin.TabularInline):
    model = SalesOrderItem
    extra = 1


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("customer__name",)
    inlines = [SalesOrderItemInline]


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("sales_order", "warehouse", "shipped_at")
    list_filter = ("warehouse", "shipped_at")