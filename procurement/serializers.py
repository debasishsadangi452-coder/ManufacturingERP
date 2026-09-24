from rest_framework import serializers
from .models import (
    Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt,
    Bill, BillLine, VendorEmail, VendorEmailAttachment, ScheduledPurchaseOrder,
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

    def validate(self, attrs):
        # Vendor and item must both belong to the requesting user's company.
        request = self.context.get("request")
        company = getattr(getattr(request, "user", None), "company", None)
        if company is not None:
            vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
            item = attrs.get("item", getattr(self.instance, "item", None))
            if vendor is not None and vendor.company_id != company.id:
                raise serializers.ValidationError({"vendor": "Vendor not found."})
            if item is not None and item.company_id != company.id:
                raise serializers.ValidationError({"item": "Item not found."})
        return attrs


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


class ScheduledPurchaseOrderSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    item_unit = serializers.CharField(source="item.unit", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True, default=None)
    purchase_order_status = serializers.CharField(source="purchase_order.status", read_only=True, default=None)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)

    class Meta:
        model = ScheduledPurchaseOrder
        fields = [
            "id", "item", "item_name", "item_unit", "quantity", "vendor", "vendor_name",
            "scheduled_date", "repeat", "status", "notes", "purchase_order", "purchase_order_status",
            "runs_count", "last_run_at", "last_message", "created_by_name", "created_at",
        ]
        read_only_fields = [
            "status", "purchase_order", "runs_count", "last_run_at", "last_message", "created_at",
        ]

    def validate_quantity(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_scheduled_date(self, value):
        from django.utils import timezone
        if value < timezone.localdate():
            raise serializers.ValidationError("Choose today or a future date.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        company = getattr(getattr(request, "user", None), "company", None)
        item = attrs.get("item", getattr(self.instance, "item", None))
        vendor = attrs.get("vendor", getattr(self.instance, "vendor", None))
        if company is not None:
            if item is not None and item.company_id != company.id:
                raise serializers.ValidationError({"item": "Item not found."})
            if vendor is not None and vendor.company_id != company.id:
                raise serializers.ValidationError({"vendor": "Vendor not found."})
        if item is not None and item.category != "raw_material":
            raise serializers.ValidationError({"item": "Only raw materials can be purchased from vendors."})
        if self.instance is not None and self.instance.status not in ("scheduled", "failed"):
            raise serializers.ValidationError(f"A {self.instance.status} schedule can no longer be changed.")
        return attrs
