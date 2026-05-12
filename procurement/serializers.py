from rest_framework import serializers
from .models import Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt
from inventory.models import Item


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = "__all__"


class VendorPriceListSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_unit = serializers.CharField(source="item.unit", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = VendorPriceList
        fields = [
            "id", "vendor", "vendor_name", "item", "item_name", "item_unit",
            "unit_price", "currency", "min_order_qty", "lead_time_days",
            "notes", "is_active", "effective_date", "updated_at",
        ]
        read_only_fields = ["effective_date", "updated_at"]


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_unit = serializers.CharField(source="item.unit", read_only=True)
    total_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = PurchaseOrderItem
        fields = [
            "id", "purchase_order", "item", "item_name", "item_unit",
            "quantity", "unit_price", "total_price",
        ]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "vendor", "vendor_name", "created_at", "expected_delivery",
            "priority", "total_amount", "status", "items",
        ]
        read_only_fields = ["total_amount", "created_at"]


class GoodsReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoodsReceipt
        fields = "__all__"
