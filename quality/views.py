from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.permission import IsQuality, IsAdmin, IsProduction, IsStore
from .models import QualityCheck, IncomingQualityCheck
from .serializers import QualityCheckSerializer, IncomingQualityCheckSerializer
from . import services
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin


def _bad(exc):
    detail = getattr(exc, "detail", exc)
    if isinstance(detail, list):
        detail = " ".join(str(d) for d in detail)
    return Response({"error": str(detail)}, status=status.HTTP_400_BAD_REQUEST)


class QualityCheckViewSet(viewsets.ModelViewSet):
    # Newest arrivals first — a batch that just reached quality is the one
    # awaiting a decision, so it belongs at the top of the queue. `-id` breaks
    # ties within the same second, which auto_now_add timestamps can collide on
    # when a run completes several checks at once.
    queryset = (
        QualityCheck.objects
        .select_related("production_order__recipe__product", "lot", "item", "vendor", "output")
        .order_by("-inspected_at", "-id")
    )
    serializer_class = QualityCheckSerializer
    permission_classes = [IsQuality | IsAdmin]
    filterset_fields = ["status", "inspection_type", "production_order", "goods_receipt"]

    def get_queryset(self):
        company = getattr(self.request.user, "company", None)
        if company is None:
            return QualityCheck.objects.none()
        # Production checks belong to the product's company; incoming checks
        # to the vendor's.
        return super().get_queryset().filter(
            Q(production_order__recipe__product__company=company) | Q(vendor__company=company)
        )

    def perform_create(self, serializer):
        order = serializer.validated_data.get("production_order")
        company = self.request.user.company
        if order is None or (company is not None and order.recipe.product.company_id not in (None, company.id)):
            raise ValidationError({"production_order": "Pick a production order of your company."})
        qc = serializer.save()
        log_activity(self.request.user, "Quality", "Create Quality Check", f"Created QC for production order #{qc.production_order.id} (test: {qc.test_type or 'N/A'})")

    # -------------------------------------------------
    # ✅ Decide (full, partial, reject, quarantine, rework)
    # -------------------------------------------------
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """Payload: accepted_quantity, rejected_quantity, quarantined_quantity,
        rework_quantity (must add up to the inspected quantity), optional
        sample_quantity, remarks, result, parameters."""
        qc = self.get_object()
        try:
            qc = services.decide(qc, request.data, request.user)
        except ValidationError as e:
            return _bad(e)
        log_activity(
            request.user, "Quality", "QA Decision",
            f"QC #{qc.id}: {qc.result_decision} (accepted {qc.accepted_quantity:g}, rejected {qc.rejected_quantity:g}, "
            f"quarantine {qc.quarantined_quantity:g}, rework {qc.rework_quantity:g})",
        )
        return Response(QualityCheckSerializer(qc).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Accept everything inspected (or `accepted_quantity`, rejecting the rest)."""
        qc = self.get_object()
        data = {k: v for k, v in request.data.items()}
        if data.get("accepted_quantity") not in (None, ""):
            accepted = float(data["accepted_quantity"])
            data.setdefault("rejected_quantity", max(qc.inspected_quantity - accepted, 0))
        else:
            data["accepted_quantity"] = qc.inspected_quantity
        try:
            qc = services.decide(qc, data, request.user)
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Quality", "Approve Batch", f"Approved QC #{qc.id} ({qc.accepted_quantity:g} accepted)")
        return Response({"status": "Batch approved", "check": QualityCheckSerializer(qc).data})

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Reject everything inspected (use `decide` for quarantine/rework)."""
        qc = self.get_object()
        data = {k: v for k, v in request.data.items()}
        data.update({"accepted_quantity": 0, "rejected_quantity": qc.inspected_quantity})
        try:
            qc = services.decide(qc, data, request.user)
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Quality", "Reject Batch", f"Rejected QC #{qc.id}. Remarks: {qc.remarks or 'None'}")
        return Response({"status": "Batch rejected", "check": QualityCheckSerializer(qc).data})

    @action(detail=True, methods=["post"])
    def send_to_rework(self, request, pk=None):
        """Create the rework production order for this check's rework lot."""
        from inventory.models import Batch
        from production.execution import create_rework_order
        from production.serializers import ProductionOrderSerializer
        qc = self.get_object()
        lot = Batch.objects.filter(parent=qc.lot, qa_status="rework").first() if qc.lot_id else None
        if lot is None and qc.lot_id and qc.lot.qa_status == "rework":
            lot = qc.lot
        if lot is None:
            return Response({"error": "This check has no quantity marked for rework."}, status=400)
        try:
            order = create_rework_order(lot, request.user, request.data.get("quantity"))
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def release_quarantine(self, request):
        """Re-inspect a quarantined lot. Payload: lot, accepted_quantity, rejected_quantity."""
        from inventory.models import Batch
        lot = Batch.objects.filter(pk=request.data.get("lot"), company=request.user.company).first()
        if lot is None:
            return Response({"error": "Lot not found."}, status=400)
        try:
            lot = services.release_quarantine(
                lot, request.data.get("accepted_quantity"), request.data.get("rejected_quantity"), request.user
            )
        except ValidationError as e:
            return _bad(e)
        return Response({"lot": lot.id, "qa_status": lot.qa_status, "remaining_quantity": lot.remaining_quantity})

    @action(detail=False, methods=["post"])
    def scrap_lot(self, request):
        """Scrap a rejected or quarantined lot (TB-18)."""
        from inventory.models import Batch
        from production.execution import record_scrap
        lot = Batch.objects.filter(pk=request.data.get("lot"), company=request.user.company).first()
        if lot is None or lot.qa_status not in ("rejected", "quarantine"):
            return Response({"error": "Only rejected or quarantined lots can be scrapped."}, status=400)
        if lot.production_order_id is None:
            return Response({"error": "Incoming material is returned to the vendor, not scrapped here."}, status=400)
        try:
            record = record_scrap(
                lot.production_order, lot.item, lot.remaining_quantity or lot.quantity, request.user,
                reason=request.data.get("reason", "QA rejected"), lot=lot, source="qa",
            )
        except ValidationError as e:
            return _bad(e)
        return Response({"scrap_record": record.id, "cost_impact": str(record.cost_impact)})

    @action(detail=False, methods=["get"], permission_classes=[IsQuality | IsAdmin | IsProduction])
    def lots(self, request):
        """QA-held lots: pending, rejected, quarantine, rework."""
        from inventory.models import Batch
        qs = Batch.objects.filter(
            company=request.user.company, qa_status__in=["pending", "rejected", "quarantine", "rework"]
        ).select_related("item", "production_order").order_by("-created_at")
        return Response([
            {
                "id": b.id, "lot": b.batch_number, "item": b.item.name, "qa_status": b.qa_status,
                "quantity": b.quantity, "remaining_quantity": b.remaining_quantity,
                "production_order": b.production_order.order_number if b.production_order_id else None,
                "parent": b.parent_id, "source": b.source,
            }
            for b in qs
        ])


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
