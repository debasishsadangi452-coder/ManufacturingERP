from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permission import IsAdmin, IsFinance, IsSales, IsStore
from core.tenancy import CompanyScopedMixin
from core.utils import log_activity
from sales.models import SalesOrder

from . import services
from .models import Dispatch, FGAllocation
from .serializers import DispatchSerializer, FGAllocationSerializer


def _bad(exc):
    detail = getattr(exc, "detail", exc)
    if isinstance(detail, dict):
        detail = " ".join(
            str(v) for value in detail.values() for v in (value if isinstance(value, list) else [value])
        )
    elif isinstance(detail, list):
        detail = " ".join(str(d) for d in detail)
    return Response({"error": str(detail)}, status=status.HTTP_400_BAD_REQUEST)


def _order(request, pk):
    order = SalesOrder.objects.filter(pk=pk, customer__company=request.user.company).first()
    if order is None:
        raise ValidationError("Customer order not found.")
    return order


class FulfilmentOrderView(APIView):
    """Order-level fulfilment: quantities per line, allocation, pick list."""
    permission_classes = [IsSales | IsStore | IsAdmin]

    def get(self, request, pk):
        try:
            order = _order(request, pk)
        except ValidationError as e:
            return _bad(e)
        return Response({
            "sales_order": order.id,
            "status": order.status,
            "customer": order.customer.name,
            "lines": services.order_fulfilment(order),
            "allocations": FGAllocationSerializer(order.fg_allocations.all(), many=True).data,
            "dispatches": DispatchSerializer(order.dispatches.all(), many=True).data,
        })


class AllocateView(APIView):
    permission_classes = [IsSales | IsStore | IsAdmin]

    def post(self, request, pk):
        try:
            order = _order(request, pk)
            created, messages = services.allocate(order, request.user, request.data.get("lines"))
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Fulfilment", "Allocate", f"SO-{order.id}: {len(created)} allocation(s)")
        return Response({
            "allocations": FGAllocationSerializer(created, many=True).data,
            "messages": messages,
            "lines": services.order_fulfilment(order),
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class FGInventoryView(APIView):
    """TB-05: finished goods by QA bucket, allocation and availability."""
    permission_classes = [IsSales | IsStore | IsAdmin | IsFinance]

    def get(self, request):
        return Response(services.fg_inventory(request.user.company))


class FGAllocationViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    company_field = "company"
    queryset = FGAllocation.objects.select_related("item", "sales_order").all()
    serializer_class = FGAllocationSerializer
    permission_classes = [IsSales | IsStore | IsAdmin]
    filterset_fields = ["sales_order", "status", "item"]

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        try:
            alloc = services.release_allocation(self.get_object(), request.user)
        except ValidationError as e:
            return _bad(e)
        return Response(FGAllocationSerializer(alloc).data)


class DispatchViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """TB-11: pick → pack → stage → prepare → verify → dispatch → deliver."""
    company_field = "company"
    queryset = Dispatch.objects.select_related("sales_order__customer", "warehouse", "invoice").prefetch_related(
        "lines__item", "lines__sales_order_item")
    serializer_class = DispatchSerializer
    permission_classes = [IsSales | IsStore | IsAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = ["sales_order", "status", "shipment_mode"]

    def create(self, request, *args, **kwargs):
        """Payload: sales_order, optional lines [{order_item_id, quantity}], warehouse."""
        from inventory.models import Warehouse
        try:
            order = _order(request, request.data.get("sales_order"))
            warehouse = None
            if request.data.get("warehouse"):
                warehouse = Warehouse.objects.filter(pk=request.data["warehouse"], company=request.user.company).first()
            dispatch = services.create_dispatch(order, request.user, request.data.get("lines"), warehouse)
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Fulfilment", "Pick List", f"{dispatch.dispatch_number} created for SO-{order.id}")
        return Response(DispatchSerializer(dispatch).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        # Logistics fields may be corrected until the goods leave.
        if serializer.instance.status in ("dispatched", "delivered", "cancelled"):
            allowed = {"received_by", "delivery_notes", "shipment_reference", "transport_details"}
            if set(serializer.validated_data) - allowed:
                raise ValidationError("Only delivery details can change after dispatch.")
        serializer.save()

    def _run(self, fn, *args, **kwargs):
        try:
            dispatch = fn(self.get_object(), *args, **kwargs)
        except ValidationError as e:
            return _bad(e)
        dispatch.refresh_from_db()
        return Response(DispatchSerializer(dispatch).data)

    @action(detail=True, methods=["post"])
    def pick(self, request, pk=None):
        return self._run(services.pick, request.data.get("lines"), user=request.user)

    @action(detail=True, methods=["post"])
    def pack(self, request, pk=None):
        return self._run(services.pack, request.data.get("packages"), request.data.get("gross_weight"), user=request.user)

    @action(detail=True, methods=["post"])
    def stage(self, request, pk=None):
        return self._run(services.stage, user=request.user)

    @action(detail=True, methods=["post"])
    def prepare(self, request, pk=None):
        return self._run(services.prepare_shipment, request.data, user=request.user)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        return self._run(services.verify, user=request.user)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        return self._run(services.confirm_dispatch, user=request.user)

    @action(detail=True, methods=["post"])
    def deliver(self, request, pk=None):
        return self._run(services.mark_delivered, request.data.get("received_by", ""),
                         request.data.get("notes", ""), user=request.user)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._run(services.cancel_dispatch, user=request.user)

    @action(detail=True, methods=["post"])
    def invoice(self, request, pk=None):
        from sales.serializers import InvoiceSerializer
        try:
            invoice = services.invoice_dispatch(self.get_object(), request.user)
        except ValidationError as e:
            return _bad(e)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)
