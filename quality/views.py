from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permission import IsQuality, IsAdmin
from .models import QualityCheck
from .serializers import QualityCheckSerializer
from core.utils import log_activity


class QualityCheckViewSet(viewsets.ModelViewSet):
    queryset = QualityCheck.objects.all()
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
