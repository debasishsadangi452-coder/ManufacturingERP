from rest_framework import serializers

from .models import Dispatch, DispatchLine, FGAllocation


class FGAllocationSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source="item.name")
    open_quantity = serializers.ReadOnlyField()
    customer_order = serializers.SerializerMethodField()

    class Meta:
        model = FGAllocation
        fields = "__all__"
        read_only_fields = [f.name for f in FGAllocation._meta.fields]

    def get_customer_order(self, obj):
        return f"SO-{obj.sales_order_id}"


class DispatchLineSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source="item.name")
    unit = serializers.ReadOnlyField(source="item.unit")
    ordered_quantity = serializers.ReadOnlyField(source="sales_order_item.quantity")

    class Meta:
        model = DispatchLine
        fields = "__all__"


class DispatchSerializer(serializers.ModelSerializer):
    lines = DispatchLineSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    shipment_mode_display = serializers.CharField(source="get_shipment_mode_display", read_only=True)
    customer_name = serializers.ReadOnlyField(source="sales_order.customer.name")
    customer_order = serializers.SerializerMethodField()
    warehouse_name = serializers.ReadOnlyField(source="warehouse.name")
    invoice_number = serializers.SerializerMethodField()
    total_quantity = serializers.ReadOnlyField()

    class Meta:
        model = Dispatch
        fields = "__all__"
        read_only_fields = [
            "company", "dispatch_number", "sales_order", "status", "picked_at", "packed_at", "staged_at",
            "prepared_at", "verified_at", "verified_by", "dispatched_at", "delivered_at", "shipment",
            "invoice", "invoice_error", "created_by", "created_at",
        ]

    def get_customer_order(self, obj):
        return f"SO-{obj.sales_order_id}"

    def get_invoice_number(self, obj):
        return f"INV-{obj.invoice_id}" if obj.invoice_id else None
