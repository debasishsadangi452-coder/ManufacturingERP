from rest_framework import serializers
from .models import QualityCheck, IncomingQualityCheck


class QualityCheckSerializer(serializers.ModelSerializer):
    inspected_quantity = serializers.ReadOnlyField()
    pending_quantity = serializers.ReadOnlyField()
    order_number = serializers.ReadOnlyField(source="production_order.order_number")
    product_name = serializers.SerializerMethodField()
    lot_code = serializers.ReadOnlyField(source="lot.batch_number")
    vendor_name = serializers.ReadOnlyField(source="vendor.name")
    customer_order = serializers.SerializerMethodField()
    inspector_name = serializers.SerializerMethodField()

    class Meta:
        model = QualityCheck
        fields = "__all__"
        read_only_fields = [
            "accepted_quantity", "rejected_quantity", "quarantined_quantity", "rework_quantity",
            "result_decision", "inspector", "decided_at", "certificate_number", "output",
        ]

    def get_product_name(self, obj):
        if obj.item_id:
            return obj.item.name
        return obj.production_order.recipe.product.name if obj.production_order_id else None

    def get_customer_order(self, obj):
        so_id = obj.production_order.sales_order_id if obj.production_order_id else None
        return f"SO-{so_id}" if so_id else None

    def get_inspector_name(self, obj):
        if not obj.inspector_id:
            return None
        return obj.inspector.get_full_name() or obj.inspector.username


class IncomingQualityCheckSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    received_warehouse_name = serializers.CharField(source="received_warehouse.name", read_only=True)
    destination_warehouse_name = serializers.CharField(source="destination_warehouse.name", read_only=True)
    inspector_username = serializers.CharField(source="inspector.username", read_only=True)
    po_id = serializers.IntegerField(source="purchase_order.id", read_only=True)
    grn_id = serializers.IntegerField(source="goods_receipt.id", read_only=True)

    class Meta:
        model = IncomingQualityCheck
        fields = [
            "id", "check_number", "company", "goods_receipt", "grn_id",
            "purchase_order", "po_id", "vendor", "vendor_name",
            "item", "item_name", "batch", "batch_number",
            "received_warehouse", "received_warehouse_name",
            "destination_warehouse", "destination_warehouse_name",
            "received_quantity", "released_quantity", "uom",
            "status", "inspector", "inspector_username", "inspection_date",
            "parameters", "remarks", "created_at", "updated_at",
        ]
        read_only_fields = ["check_number", "created_at", "updated_at"]
