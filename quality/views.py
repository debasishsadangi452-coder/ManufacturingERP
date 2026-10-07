from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.permission import IsQuality, IsAdmin, IsProduction
from .models import QualityCheck
from .serializers import QualityCheckSerializer
from . import services
from core.utils import log_activity


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
