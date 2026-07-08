from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend

from .models import Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt
from .serializers import (
    VendorSerializer, VendorPriceListSerializer,
    PurchaseOrderSerializer, PurchaseOrderItemSerializer, GoodsReceiptSerializer,
)
from inventory.services import increase_stock
from accounts.permission import IsStore, IsAdmin
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin


class VendorViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer
    permission_classes = [IsStore | IsAdmin]

    def perform_create(self, serializer):
        vendor = serializer.save(company=self.request.user.company)
        log_activity(self.request.user, "Procurement", "Create Vendor", f"Created vendor '{vendor.name}'")

    def perform_destroy(self, instance):
        log_activity(self.request.user, "Procurement", "Delete Vendor", f"Deleted vendor '{instance.name}'")
        instance.delete()

    @action(detail=True, methods=["get"], url_path="price-list")
    def price_list(self, request, pk=None):
        """Return the full price catalogue for this vendor."""
        vendor = self.get_object()
        qs = VendorPriceList.objects.filter(vendor=vendor, is_active=True).select_related("item")
        return Response(VendorPriceListSerializer(qs, many=True).data)


class VendorPriceListViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "vendor__company"
    """
    store_user manages per-vendor per-item unit prices.
    GET  /api/procurement/vendor-prices/?vendor=<id>  → prices for a vendor
    GET  /api/procurement/vendor-prices/?item=<id>    → all vendor prices for an item
    POST /api/procurement/vendor-prices/              → create/set a price
    """
    queryset = VendorPriceList.objects.select_related("vendor", "item").all()
    serializer_class = VendorPriceListSerializer
    permission_classes = [IsStore | IsAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["vendor", "item", "is_active"]

    def perform_create(self, serializer):
        price = serializer.save()
        log_activity(
            self.request.user, "Procurement", "Set Vendor Price",
            f"Set {price.vendor.name} → {price.item.name} @ {price.currency} {price.unit_price}/unit"
        )

    def perform_update(self, serializer):
        price = serializer.save()
        log_activity(
            self.request.user, "Procurement", "Update Vendor Price",
            f"Updated {price.vendor.name} → {price.item.name} → {price.currency} {price.unit_price}/unit"
        )


class PurchaseOrderViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "vendor__company"
    queryset = PurchaseOrder.objects.prefetch_related("items__item").select_related("vendor").all()
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsStore | IsAdmin]

    def perform_create(self, serializer):
        po = serializer.save()
        log_activity(
            self.request.user, "Procurement", "Create Purchase Order",
            f"Created PO #{po.id} from vendor '{po.vendor.name}' (status: {po.status})"
        )

    def perform_update(self, serializer):
        instance = self.get_object()
        new_status = serializer.validated_data.get("status")
        if new_status == "approved" and instance.status != "approved":
            if not self.request.user.role == "admin":
                # Check if it was auto-approved via an action, otherwise prevent manual status change to approved by non-admins
                if not getattr(instance, '_auto_approved', False):
                    raise ValidationError({"error": "Only admins can manually approve purchase orders."})
        po = serializer.save()
        if new_status and new_status != instance.status:
            log_activity(
                self.request.user, "Procurement", "Update Purchase Order Status",
                f"PO #{po.id} status changed from '{instance.status}' to '{new_status}'"
            )

    @action(detail=True, methods=["post"])
    def request_approval(self, request, pk=None):
        """
        Store user requests approval for a PO.
        Logic: Auto-approve if total_amount <= user.auto_approve_limit
        """
        po = self.get_object()
        if po.status != "draft":
            return Response({"error": f"Can only request approval for draft orders. Current status: {po.status}"}, status=status.HTTP_400_BAD_REQUEST)
        
        user = request.user
        limit = getattr(user, 'auto_approve_limit', 0)
        
        if po.total_amount <= limit and limit > 0:
            po.status = "approved"
            po._auto_approved = True
            po.save()
            log_activity(user, "Procurement", "Auto Approve PO", f"PO #{po.id} ($ {po.total_amount}) auto-approved based on user individual limit ($ {limit})")
            return Response({"status": "approved", "message": "Order auto-approved based on your individual budget limit."})
        else:
            po.status = "pending"
            po.save()
            log_activity(user, "Procurement", "Request PO Approval", f"PO #{po.id} ($ {po.total_amount}) sent to admin for approval (User limit: $ {limit})")
            return Response({"status": "pending", "message": "Order sent to admin for approval."})


class PurchaseOrderItemViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "purchase_order__vendor__company"
    queryset = PurchaseOrderItem.objects.select_related("item", "purchase_order").all()
    serializer_class = PurchaseOrderItemSerializer
    permission_classes = [IsStore | IsAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["purchase_order"]

    def perform_create(self, serializer):
        poi = serializer.save()
        log_activity(
            self.request.user, "Procurement", "Add PO Item",
            f"Added {poi.quantity} x '{poi.item.name}' @ {poi.unit_price}/unit to PO #{poi.purchase_order.id}"
        )


class GoodsReceiptViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "purchase_order__vendor__company"
    queryset = GoodsReceipt.objects.all()
    serializer_class = GoodsReceiptSerializer
    permission_classes = [IsStore | IsAdmin]

    def perform_create(self, serializer):
        po = serializer.validated_data["purchase_order"]
        if po.status not in ["approved", "ordered"]:
            raise ValidationError({
                "error": f"Cannot receive goods for PO #{po.id}. "
                         f"Status must be 'approved' or 'ordered', currently '{po.status}'."
            })
        receipt = serializer.save()
        items_received = []
        for poi in po.items.all():
            increase_stock(
                poi.item, receipt.warehouse, poi.quantity,
                user=self.request.user, reference=f"GRN PO#{po.id}",
            )
            items_received.append(f"{poi.quantity} x {poi.item.name}")
        po.status = "received"
        po.save()
        log_activity(
            self.request.user, "Procurement", "Goods Receipt",
            f"Received goods for PO #{po.id} into '{receipt.warehouse.name}': {', '.join(items_received)}"
        )
