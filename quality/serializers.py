from rest_framework import serializers
from .models import QualityCheck, IncomingQualityCheck


class QualityCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = QualityCheck
        fields = "__all__"


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
