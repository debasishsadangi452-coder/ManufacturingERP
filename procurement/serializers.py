from rest_framework import serializers
from .models import Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt, Bill, BillLine
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

    def validate_item(self, item):
        if item.category != "raw_material":
            raise serializers.ValidationError(
                "Only raw materials can be assigned to vendors for purchasing."
            )
        return item


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

    def validate_item(self, item):
        if item.category != "raw_material":
            raise serializers.ValidationError(
                "Only raw materials can be purchased from vendors."
            )
        return item


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "vendor", "vendor_name", "created_at", "expected_delivery",
            "priority", "total_amount", "status", "items", "quickbooks_id",
        ]
        read_only_fields = ["total_amount", "created_at", "quickbooks_id"]


class GoodsReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoodsReceipt
        fields = "__all__"


class BillLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = BillLine
        fields = ["id", "item", "item_name", "description", "quantity", "unit_price", "amount"]


class BillSerializer(serializers.ModelSerializer):
    lines = BillLineSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)

    class Meta:
        model = Bill
        fields = [
            "id", "purchase_order", "vendor", "vendor_name", "bill_number",
            "bill_date", "due_date", "total_amount", "status",
            "quickbooks_id", "quickbooks_last_synced_at", "created_at", "lines",
        ]
