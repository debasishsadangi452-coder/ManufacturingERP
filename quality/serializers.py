from rest_framework import serializers
from .models import QualityCheck


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
