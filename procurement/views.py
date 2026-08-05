from datetime import date

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend

from .models import Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt, Bill, BillLine
from .serializers import (
    VendorSerializer, VendorPriceListSerializer,
    PurchaseOrderSerializer, PurchaseOrderItemSerializer, GoodsReceiptSerializer,
    BillSerializer,
)
from inventory.models import Item
from inventory.serializers import ItemSerializer
from inventory.services import increase_stock
from accounts.permission import IsStore, IsAdmin, IsFinance, IsQuality
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

    @action(detail=False, methods=["get"])
    def available_items(self, request):
        """List only raw materials (inventory items) available for purchase orders."""
        company = request.user.company
        items = (
            Item.objects.filter(company=company, category="raw_material")
            .exclude(erp_classification="out_of_scope")
            .order_by("name")
        )
        return Response(ItemSerializer(items, many=True).data)


class BillViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Vendor bills (Accounts Payable). Created from a purchase order via
    POST /api/procurement/bills/from_purchase_order/ and mirrored to
    QuickBooks automatically.
    """
    company_field = "company"
    queryset = Bill.objects.select_related("vendor", "purchase_order").prefetch_related("lines__item")
    serializer_class = BillSerializer
    permission_classes = [IsStore | IsAdmin | IsFinance]

    @action(detail=False, methods=["post"])
    def from_purchase_order(self, request):
        """
        Payload: { "purchase_order": 12, "bill_number": "XYZ-991",
                   "bill_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD" }
        Lines and total are copied from the purchase order.
        """
        po = PurchaseOrder.objects.filter(
            id=request.data.get("purchase_order"), vendor__company=request.user.company
        ).prefetch_related("items__item").first()
        if not po:
            return Response({"error": "Purchase order not found."}, status=status.HTTP_404_NOT_FOUND)
        if po.status in ("draft", "cancelled"):
            return Response(
                {"error": f"Cannot bill a {po.status} purchase order."},
                status=status.HTTP_400_BAD_REQUEST
            )
        existing = po.bills.exclude(status="cancelled").first()
        if existing:
            return Response(
                {"error": f"Bill {existing.bill_number or existing.id} already exists for this PO."},
                status=status.HTTP_400_BAD_REQUEST
            )

        def parse_date(field, default=None):
            raw = request.data.get(field)
            if not raw:
                return default
            return date.fromisoformat(str(raw))

        try:
            bill_date = parse_date("bill_date", date.today())
            due_date = parse_date("due_date")
        except ValueError:
            return Response({"error": "Dates must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        bill = Bill.objects.create(
            company=po.vendor.company,
            purchase_order=po,
            vendor=po.vendor,
            bill_number=request.data.get("bill_number", ""),
            bill_date=bill_date,
            due_date=due_date,
            total_amount=po.total_amount,
        )
        for po_item in po.items.all():
            BillLine.objects.create(
                bill=bill,
                item=po_item.item,
                description=po_item.item.name,
                quantity=po_item.quantity,
                unit_price=po_item.unit_price,
                amount=po_item.total_price,
            )

        # Mirror to QuickBooks (Level 1 sync); failures land in sync errors.
        from quickbooks.push import get_active_connection, safe_push
        connection = get_active_connection(po.vendor.company)
        if connection:
            safe_push(connection, "bill", bill)

        log_activity(
            request.user, "Procurement", "Record Bill",
            f"Recorded bill '{bill.bill_number or bill.id}' for PO #{po.id} (total: {bill.total_amount})"
        )
        return Response(BillSerializer(bill).data, status=status.HTTP_201_CREATED)


class GoodsReceiptViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "purchase_order__vendor__company"
    queryset = GoodsReceipt.objects.all()
    serializer_class = GoodsReceiptSerializer
    # Quality verifies received quantities and records lot codes at receiving,
    # which is where the SQF lot chain starts.
    permission_classes = [IsStore | IsAdmin | IsQuality]

    def perform_create(self, serializer):
        po = serializer.validated_data["purchase_order"]
        if po.status not in ["approved", "ordered"]:
            raise ValidationError({
                "error": f"Cannot receive goods for PO #{po.id}. "
                         f"Status must be 'approved' or 'ordered', currently '{po.status}'."
            })
        receipt = serializer.save()
        items_received = []
        from inventory.lots import create_raw_lot
        for poi in po.items.all():
            increase_stock(
                poi.item, receipt.warehouse, poi.quantity,
                user=self.request.user, reference=f"GRN PO#{po.id}",
            )
            # 🔗 SQF traceability: every received line becomes a raw lot tied to
            # this goods receipt (and thus the vendor delivery).
            create_raw_lot(
                poi.item, receipt.warehouse, poi.quantity, receipt,
                company=getattr(poi.item, "company", None),
            )
            items_received.append(f"{poi.quantity} x {poi.item.name}")
        po.status = "received"
        po.save()
        from finance.services import record_procurement_cost
        record_procurement_cost(po, user=self.request.user)
        log_activity(
            self.request.user, "Procurement", "Goods Receipt",
            f"Received goods for PO #{po.id} into '{receipt.warehouse.name}': {', '.join(items_received)}"
        )
