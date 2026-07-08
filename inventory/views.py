from rest_framework import viewsets, status
from .models import Item, Warehouse, Stock, Batch, InventoryRequest, StockMovement
from .serializers import (
    ItemSerializer,
    WarehouseSerializer,
    StockSerializer,
    BatchSerializer,
    InventoryRequestSerializer,
    StockMovementSerializer,
)
from accounts.permission import IsStore, IsAdmin, IsProduction
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from .services import adjust_stock
from .models import Item, Warehouse
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin


class ItemViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def perform_create(self, serializer):
        warehouse_id = serializer.validated_data.get('warehouse_id')
        initial_qty = serializer.validated_data.get('initial_quantity', 0)
        
        item = serializer.save(company=self.request.user.company)
        
        if warehouse_id:
            warehouse = Warehouse.objects.get(id=warehouse_id)
            # Use increase_stock to handle logs and stuff
            from .services import increase_stock
            increase_stock(item, warehouse, initial_qty, user=self.request.user, reference="Initial Stock Registration")
            log_activity(self.request.user, "Inventory", "Create Item", f"Created item '{item.name}' with {initial_qty} units in warehouse '{warehouse.name}'")
        else:
            log_activity(self.request.user, "Inventory", "Create Item", f"Created item '{item.name}' (category: {item.category})")

    def perform_destroy(self, instance):
        log_activity(self.request.user, "Inventory", "Delete Item", f"Deleted item '{instance.name}'")
        instance.delete()


class WarehouseViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def perform_create(self, serializer):
        wh = serializer.save(company=self.request.user.company)
        log_activity(self.request.user, "Inventory", "Create Warehouse", f"Created warehouse '{wh.name}'")

    @action(detail=True, methods=["post"])
    def transfer_delete(self, request, pk=None):
        source_wh = self.get_object()
        dest_wh_id = request.data.get("destination_warehouse")
        
        if not dest_wh_id:
            return Response({"error": "Destination warehouse required"}, status=400)
            
        dest_wh = Warehouse.objects.get(id=dest_wh_id)
        
        # Move all stock
        stocks = Stock.objects.filter(warehouse=source_wh)
        for stock in stocks:
            if stock.quantity > 0:
                from .services import increase_stock
                increase_stock(stock.item, dest_wh, stock.quantity, user=request.user, reference=f"Transfer from deleted WH: {source_wh.name}")
            stock.delete()
            
        log_activity(request.user, "Inventory", "Transfer & Delete Warehouse", f"Transferred all stock from '{source_wh.name}' to '{dest_wh.name}' and deleted source warehouse")
        source_wh.delete()
        return Response({"status": f"Warehouse {source_wh.name} deleted and stock transferred to {dest_wh.name}"})


class BatchViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "item__company"
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]


class StockViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "item__company"
    queryset = Stock.objects.all()
    serializer_class = StockSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    # Block direct creation to force process flow
    def create(self, request, *args, **kwargs):
        return Response({"error": "Direct stock creation disabled. Use Procurement or Production flow."}, status=403)

    @action(detail=False, methods=["post"])
    def adjust(self, request):
        item_id = request.data.get("item")
        warehouse_id = request.data.get("warehouse")
        qty = float(request.data.get("quantity"))
        reason = request.data.get("reason", "Manual Adjustment")

        item = Item.objects.get(id=item_id)
        warehouse = Warehouse.objects.get(id=warehouse_id)

        try:
            adjust_stock(item, warehouse, qty, user=request.user, reference=reason)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

        log_activity(request.user, "Inventory", "Stock Adjustment", f"Adjusted '{item.name}' in '{warehouse.name}' to {qty} units. Reason: {reason}")
        return Response({"status": "Adjustment successful"})

    @action(detail=False, methods=["post"])
    def transfer(self, request):
        item_id = request.data.get("item")
        source_id = request.data.get("from_warehouse")
        dest_id = request.data.get("to_warehouse")
        qty = float(request.data.get("quantity"))

        if source_id == dest_id:
            return Response({"error": "Source and destination must be different"}, status=400)

        item = Item.objects.get(id=item_id)
        source = Warehouse.objects.get(id=source_id)
        dest = Warehouse.objects.get(id=dest_id)

        from .services import decrease_stock, increase_stock
        try:
            decrease_stock(item, source, qty, user=request.user, reference=f"Transfer to {dest.name}")
            increase_stock(item, dest, qty, user=request.user, reference=f"Transfer from {source.name}")
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

        log_activity(request.user, "Inventory", "Stock Transfer", f"Transferred {qty} units of '{item.name}' from '{source.name}' to '{dest.name}'")
        return Response({"status": "Transfer successful"})

class InventoryRequestViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "item__company"
    serializer_class = InventoryRequestSerializer
    permission_classes = [IsStore | IsAdmin | IsProduction]

    def get_queryset(self):
        user = self.request.user
        qs = InventoryRequest.objects.filter(item__company=user.company).order_by('-created_at')
        # Production users only see requests tied to their own production orders
        if getattr(user, 'role', None) == 'production':
            qs = qs.filter(production_order__isnull=False)
        return qs

    def perform_create(self, serializer):
        req = serializer.save()
        log_activity(self.request.user, "Inventory", "Inventory Request", f"Created inventory request #{req.id} for '{req.item.name}' (qty: {req.quantity})")

    @action(detail=True, methods=['post'])
    def supply(self, request, pk=None):
        req = self.get_object()
        if req.status not in ['pending', 'procuring']:
            return Response({"error": f"Request is already {req.status}"}, status=400)

        from .services import decrease_stock

        # Check stock in the request's warehouse before attempting deduction
        warehouse_stock = Stock.objects.filter(item=req.item, warehouse=req.warehouse).first()
        available = warehouse_stock.quantity if warehouse_stock else 0

        if available < req.quantity:
            # Find total stock across all warehouses for context
            all_stock = Stock.objects.filter(item=req.item)
            other_warehouses = [
                f"{s.warehouse.name} ({s.quantity} {req.item.unit})"
                for s in all_stock
                if s.warehouse_id != req.warehouse_id and s.quantity > 0
            ]
            hint = f" Stock is available in: {', '.join(other_warehouses)}." if other_warehouses else " No stock found in any warehouse."
            return Response({
                "error": (
                    f"'{req.warehouse.name}' only has {available} {req.item.unit} of {req.item.name}, "
                    f"but {req.quantity} {req.item.unit} were requested.{hint} "
                    f"Transfer stock to '{req.warehouse.name}' first, or adjust the request quantity."
                )
            }, status=400)

        try:
            decrease_stock(
                req.item,
                req.warehouse,
                req.quantity,
                user=request.user,
                reference=f"Fulfilling Request #{req.id} for Production Order #{req.production_order_id}"
            )
            req.status = 'supplied'
            req.save()
            log_activity(request.user, "Inventory", "Supply Materials", f"Supplied {req.quantity} of '{req.item.name}' for Production Order #{req.production_order_id}")
            return Response({"status": "Materials supplied successfully"})
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'])
    def procure(self, request, pk=None):
        req = self.get_object()
        if req.status != 'pending':
            return Response({"error": f"Only pending requests can be sent to procurement. Current status: {req.status}"}, status=400)

        if req.item.category != "raw_material":
            return Response(
                {"error": f"Only raw materials can be auto-procured. '{req.item.name}' is not a raw material."},
                status=400
            )

        from procurement.models import VendorPriceList, PurchaseOrder, PurchaseOrderItem
        price_entry = (
            VendorPriceList.objects
            .filter(item=req.item, is_active=True)
            .select_related("vendor")
            .order_by("unit_price", "lead_time_days")
            .first()
        )
        if not price_entry:
            return Response({"error": f"No active vendor price list found for {req.item.name}. Setup vendor prices first."}, status=400)

        order_quantity = max(float(req.quantity), float(price_entry.min_order_qty or 0))
        po = PurchaseOrder.objects.create(
            vendor=price_entry.vendor,
            status='pending',
            priority='high'
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            item=req.item,
            quantity=order_quantity,
            unit_price=price_entry.unit_price
        )
        req.status = 'procuring'
        req.save()
        log_activity(
            request.user,
            "Inventory",
            "Auto Procurement",
            f"Created PO #{po.id} for raw material '{req.item.name}' via {price_entry.vendor.name} for Request #{req.id} (qty: {order_quantity}, status: pending)"
        )
        return Response(
            {
                "status": "procuring",
                "po_id": po.id,
                "vendor": price_entry.vendor.name,
                "ordered_quantity": order_quantity,
                "message": "Procurement request created successfully. Inventory request remains open until goods are received and supplied.",
            }
        )


class StockMovementViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    company_field = "item__company"
    serializer_class = StockMovementSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def get_queryset(self):
        qs = StockMovement.objects.filter(item__company=self.request.user.company).select_related('item', 'warehouse', 'created_by').order_by('-created_at')
        item_id = self.request.query_params.get('item')
        if item_id:
            qs = qs.filter(item_id=item_id)
        return qs
