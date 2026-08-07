from rest_framework import serializers
from .models import (
    Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt,
    Bill, BillLine, VendorEmail, VendorEmailAttachment,
)
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
    item_sku = serializers.CharField(source="item.sku", read_only=True)
    total_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = PurchaseOrderItem
        fields = [
            "id", "purchase_order", "item", "item_name", "item_unit", "item_sku",
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
    vendor_email = serializers.CharField(source="vendor.email", read_only=True)
    vendor_payment_terms = serializers.CharField(source="vendor.payment_terms", read_only=True)
    email_count = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "vendor", "vendor_name", "vendor_email", "vendor_payment_terms",
            "created_at", "expected_delivery", "priority", "total_amount",
            "status", "notes", "items", "email_count", "quickbooks_id",
        ]
        read_only_fields = ["total_amount", "created_at", "quickbooks_id"]

    def get_email_count(self, obj):
        return obj.emails.count()


class VendorEmailAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = VendorEmailAttachment
        fields = ["id", "email", "file", "filename", "uploaded_at"]
        read_only_fields = ["uploaded_at"]


class VendorEmailSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    attachments = VendorEmailAttachmentSerializer(many=True, read_only=True)
    purchase_order_numbers = serializers.SerializerMethodField()
    sent_by_name = serializers.CharField(source="sent_by.username", read_only=True)

    class Meta:
        model = VendorEmail
        fields = [
            "id", "vendor", "vendor_name", "purchase_orders", "purchase_order_numbers",
            "to_email", "cc", "bcc", "subject", "body_html", "body_edited",
            "status", "created_at", "updated_at", "sent_at", "sent_by",
            "sent_by_name", "error_message", "attachments",
        ]
        # Delivery state is owned by the (future) transport, never the client.
        read_only_fields = [
            "created_at", "updated_at", "sent_at", "sent_by", "error_message", "status",
        ]

    def get_purchase_order_numbers(self, obj):
        return [f"PO-{po.id:04d}" for po in obj.purchase_orders.all()]

    def update(self, instance, validated_data):
        # Any edit to the body pins it, so later regeneration leaves it alone.
        if "body_html" in validated_data and validated_data["body_html"] != instance.body_html:
            instance.body_edited = True
        return super().update(instance, validated_data)


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
