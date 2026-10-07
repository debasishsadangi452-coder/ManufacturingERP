from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permission import IsQuality, IsAdmin, IsStore
from .models import QualityCheck, IncomingQualityCheck
from .serializers import QualityCheckSerializer, IncomingQualityCheckSerializer
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin


class QualityCheckViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "production_order__recipe__product__company"
    # Newest arrivals first — a batch that just reached quality is the one
    # awaiting a decision, so it belongs at the top of the queue. `-id` breaks
    # ties within the same second, which auto_now_add timestamps can collide on
    # when a run completes several checks at once.
    queryset = (
        QualityCheck.objects
        .select_related("production_order__recipe__product", "lot")
        .order_by("-inspected_at", "-id")
    )
    serializer_class = QualityCheckSerializer
    permission_classes = [IsQuality | IsAdmin]

    def perform_create(self, serializer):
        qc = serializer.save()
        log_activity(self.request.user, "Quality", "Create Quality Check", f"Created QC for production order #{qc.production_order.id} (test: {qc.test_type or 'N/A'})")

    # -------------------------------------------------
    # ✅ Approve Batch
    # -------------------------------------------------
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):

        qc = self.get_object()

        # Prevent double decision
        if qc.status in ["approved", "rejected"]:
            return Response(
                {"error": "Quality decision already made"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Ensure production completed
        if qc.production_order.status != "completed":
            return Response(
                {"error": "Production not completed yet"},
                status=status.HTTP_400_BAD_REQUEST
            )

        qc.status = "approved"
        qc.save()

        log_activity(request.user, "Quality", "Approve Batch", f"Approved QC #{qc.id} for production order #{qc.production_order.id} ({qc.production_order.recipe.product.name})")
        return Response({"status": "Batch approved"})

    # -------------------------------------------------
    # ❌ Reject Batch
    # -------------------------------------------------
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):

        qc = self.get_object()

        # Prevent double decision
        if qc.status in ["approved", "rejected"]:
            return Response(
                {"error": "Quality decision already made"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if qc.production_order.status != "completed":
            return Response(
                {"error": "Production not completed yet"},
                status=status.HTTP_400_BAD_REQUEST
            )

        qc.status = "rejected"
        qc.save()

        log_activity(request.user, "Quality", "Reject Batch", f"Rejected QC #{qc.id} for production order #{qc.production_order.id} ({qc.production_order.recipe.product.name}). Remarks: {qc.remarks or 'None'}")
        return Response({"status": "Batch rejected"})


class IncomingQualityCheckViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Incoming Raw Material Quality Control & Quarantine Gate.
    Raw materials received into quarantine are isolated from usable production stock.
    Only when an authorized inspector grants QC PASS does the material release into
    the production warehouse as usable available inventory.
    """
    company_field = "company"
    queryset = (
        IncomingQualityCheck.objects
        .select_related(
            "company", "goods_receipt", "purchase_order", "vendor",
            "item", "batch", "received_warehouse", "destination_warehouse", "inspector"
        )
        .order_by("-created_at")
    )
    serializer_class = IncomingQualityCheckSerializer
    permission_classes = [IsQuality | IsAdmin | IsStore]

    @action(detail=True, methods=["post"], url_path="pass")
    def pass_inspection(self, request, pk=None):
        """
        QC PASS: Releases quarantined raw material into usable inventory.
        Decreases stock in quarantine warehouse, increases stock in destination warehouse,
        updates batch status to 'available', and notifies waiting production.
        Prevents double-release atomically.
        """
        from django.db import transaction
        from django.utils import timezone
        from inventory.services import decrease_stock, increase_stock
        from procurement.views import GoodsReceiptViewSet

        with transaction.atomic():
            qc = IncomingQualityCheck.objects.select_for_update().get(pk=pk)

            # Prevent double release
            if qc.status == "passed":
                return Response(
                    {"error": "Inspection has already been passed and released. Double release prevented."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            max_releasable = qc.received_quantity - qc.released_quantity
            qty_param = request.data.get("quantity")
            qty_to_release = float(qty_param) if qty_param is not None else max_releasable

            if qty_to_release <= 0:
                return Response(
                    {"error": "Released quantity must be greater than zero."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if qty_to_release > max_releasable:
                return Response(
                    {"error": f"Released quantity ({qty_to_release}) cannot exceed available quarantine quantity ({max_releasable})."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if (qc.released_quantity + qty_to_release) > qc.received_quantity:
                return Response(
                    {"error": f"Released quantity cannot exceed received quantity ({qc.received_quantity})."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 1. Decrease stock from quarantine warehouse
            decrease_stock(
                qc.item,
                qc.received_warehouse,
                qty_to_release,
                user=request.user,
                reference=f"QC PASS Release {qc.check_number}"
            )

            # 2. Increase stock in production usable destination warehouse
            target_wh = qc.destination_warehouse or qc.received_warehouse
            increase_stock(
                qc.item,
                target_wh,
                qty_to_release,
                user=request.user,
                reference=f"QC PASS Available {qc.check_number}"
            )

            # 3. Update Batch lot to available status
            if qc.batch:
                qc.batch.warehouse = target_wh
                qc.batch.status = "available"
                qc.batch.quarantine_released_at = timezone.now()
                qc.batch.save(update_fields=["warehouse", "status", "quarantine_released_at"])

            # 4. Update IncomingQualityCheck
            qc.status = "passed"
            qc.released_quantity += qty_to_release
            qc.inspector = request.user
            qc.inspection_date = timezone.now()
            if request.data.get("remarks"):
                qc.remarks = request.data["remarks"]
            qc.save()

            # 5. Notify waiting production orders now that materials are released into available stock
            try:
                GoodsReceiptViewSet._notify_waiting_production(qc.purchase_order, qc.goods_receipt)
            except Exception:
                pass

            log_activity(
                request.user,
                "Quality",
                "Incoming QC Pass",
                f"QC PASS {qc.check_number}: Released {qty_to_release} {qc.uom} of '{qc.item.name}' into available stock in '{target_wh.name}'"
            )

        return Response(
            {
                "message": f"QC PASS: {qty_to_release} {qc.uom} released to available inventory.",
                "check": IncomingQualityCheckSerializer(qc).data
            },
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"], url_path="fail")
    def fail_inspection(self, request, pk=None):
        """
        QC FAIL: Marks incoming material as failed/rejected.
        Material remains strictly in quarantine / rejected status and is NOT released to usable stock.
        """
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            qc = IncomingQualityCheck.objects.select_for_update().get(pk=pk)

            if qc.status == "passed":
                return Response(
                    {"error": "Cannot fail an inspection that has already been passed and released to inventory."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            qc.status = "failed"
            qc.inspector = request.user
            qc.inspection_date = timezone.now()
            if request.data.get("remarks"):
                qc.remarks = request.data["remarks"]
            qc.save()

            if qc.batch:
                qc.batch.status = "rejected"
                qc.batch.save(update_fields=["status"])

            log_activity(
                request.user,
                "Quality",
                "Incoming QC Fail",
                f"QC FAIL {qc.check_number}: Rejected {qc.received_quantity} {qc.uom} of '{qc.item.name}' from vendor '{qc.vendor.name if qc.vendor else 'N/A'}'. Remarks: {qc.remarks}"
            )

        return Response(
            {
                "message": "QC FAIL: Material rejected. Quarantined material will not be released to production.",
                "check": IncomingQualityCheckSerializer(qc).data
            },
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=["post"], url_path="hold")
    def hold_inspection(self, request, pk=None):
        """
        QC HOLD: Retains material under quarantine review.
        Material remains unavailable for production until further notice.
        """
        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            qc = IncomingQualityCheck.objects.select_for_update().get(pk=pk)

            if qc.status == "passed":
                return Response(
                    {"error": "Cannot place on hold an inspection that has already been passed and released."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            qc.status = "hold"
            qc.inspector = request.user
            qc.inspection_date = timezone.now()
            if request.data.get("remarks"):
                qc.remarks = request.data["remarks"]
            qc.save()

            if qc.batch:
                qc.batch.status = "hold"
                qc.batch.save(update_fields=["status"])

            log_activity(
                request.user,
                "Quality",
                "Incoming QC Hold",
                f"QC HOLD {qc.check_number}: Put {qc.received_quantity} {qc.uom} of '{qc.item.name}' on hold. Remarks: {qc.remarks}"
            )

        return Response(
            {
                "message": "QC HOLD: Material placed on hold in quarantine.",
                "check": IncomingQualityCheckSerializer(qc).data
            },
            status=status.HTTP_200_OK
        )
