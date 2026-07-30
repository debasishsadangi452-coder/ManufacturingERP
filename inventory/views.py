from rest_framework import viewsets, status
from .models import Item, Warehouse, Stock, Batch, InventoryRequest, StockMovement, QuickBooksOnboarding, SalesQuickBooksConfig, ProcurementQuickBooksConfig, BOM, BOMLine, UnitOfMeasure, StockTransfer, CycleCount, CycleCountLine
from .serializers import (
    UnitOfMeasureSerializer,
    StockTransferSerializer,
    CycleCountSerializer,
    CycleCountLineSerializer,
    ItemSerializer,
    WarehouseSerializer,
    StockSerializer,
    BatchSerializer,
    InventoryRequestSerializer,
    StockMovementSerializer,
    QuickBooksOnboardingSerializer,
    SalesQuickBooksConfigSerializer,
    ProcurementQuickBooksConfigSerializer,
    BOMSerializer,
    BOMLineSerializer,
)
from accounts.permission import IsStore, IsAdmin, IsProduction
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from .services import adjust_stock
from .models import Item, Warehouse
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin


class UnitOfMeasureViewSet(viewsets.ModelViewSet):
    """Units of measure. Standard units are shared (company=NULL); tenants may
    add their own. The list returns both. `needs_review` surfaces items whose
    unit hasn't been mapped yet (the backfill review screen)."""
    serializer_class = UnitOfMeasureSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def get_queryset(self):
        company = getattr(self.request.user, "company", None)
        from django.db.models import Q
        return UnitOfMeasure.objects.filter(Q(company=None) | Q(company=company))

    def perform_create(self, serializer):
        serializer.save(company=self.request.user.company)

    @action(detail=False, methods=["get"])
    def needs_review(self, request):
        """Items in this company that still lack a base_unit — the backfill
        review queue. Frontend lets an admin assign units here."""
        company = request.user.company
        items = Item.objects.filter(company=company, base_unit__isnull=True).exclude(
            erp_classification="out_of_scope"
        )
        return Response([
            {"id": i.id, "name": i.name, "legacy_unit": i.unit, "sku": i.sku}
            for i in items
        ])


class ItemViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def get_queryset(self):
        """Hide items classified as out-of-scope from the ERP.

        Out-of-scope items stay in QuickBooks only and must not appear in
        inventory/procurement/production/sales. Pass ?include_out_of_scope=1
        to include them (used by tooling that needs the full list).
        """
        qs = super().get_queryset()
        if self.request.query_params.get("include_out_of_scope") not in ("1", "true", "True"):
            qs = qs.exclude(erp_classification="out_of_scope")
        return qs

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

    @action(detail=True, methods=["get"])
    def trace_back(self, request, pk=None):
        """Ingredient genealogy: the raw lots that went into this finished lot."""
        from .lots import trace_backward
        lot = self.get_object()
        return Response({
            "lot": lot.batch_number,
            "item": lot.item.name,
            "source": lot.source,
            "production_order": lot.production_order_id,
            "consumed_raw_lots": trace_backward(lot),
        })

    @action(detail=True, methods=["get"])
    def trace_forward(self, request, pk=None):
        """Recall reach: the finished lots and shipments a raw lot ended up in."""
        from .lots import trace_forward
        lot = self.get_object()
        return Response({
            "lot": lot.batch_number,
            "item": lot.item.name,
            "downstream": trace_forward(lot),
        })


class StockTransferViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Move stock between warehouses (plant → Milton staging → cold storage).

    Creating a transfer executes it immediately (decrement source, increment
    dest, carry lots). `complete` is exposed for a two-step flow if needed."""
    company_field = "company"
    queryset = StockTransfer.objects.all()
    serializer_class = StockTransferSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def perform_create(self, serializer):
        from .operations import complete_transfer
        transfer = serializer.save(
            company=self.request.user.company, created_by=self.request.user
        )
        complete_transfer(transfer, user=self.request.user)
        log_activity(
            self.request.user, "Inventory", "Stock Transfer",
            f"Transferred {transfer.quantity} x '{transfer.item.name}' "
            f"from '{transfer.source_warehouse.name}' to '{transfer.dest_warehouse.name}'",
        )

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        from .operations import complete_transfer
        transfer = complete_transfer(self.get_object(), user=request.user)
        return Response(StockTransferSerializer(transfer).data)


class CycleCountViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Physical inventory reconciliation — replaces the weekly manual count.

    Flow: create a count for a warehouse → add_line per item (system qty is
    snapshotted) → post to apply variances as stock adjustments."""
    company_field = "company"
    queryset = CycleCount.objects.all()
    serializer_class = CycleCountSerializer
    permission_classes = [IsStore | IsProduction | IsAdmin]

    def perform_create(self, serializer):
        serializer.save(company=self.request.user.company, created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def add_line(self, request, pk=None):
        """Add a counted item to the count. Snapshots current on-hand as the
        system quantity so the variance is stable."""
        count = self.get_object()
        if count.status != "open":
            return Response({"error": f"Count is {count.status}."}, status=status.HTTP_400_BAD_REQUEST)
        item = Item.objects.get(id=request.data.get("item"))
        stock = Stock.objects.filter(item=item, warehouse=count.warehouse).first()
        line, _ = CycleCountLine.objects.update_or_create(
            cycle_count=count, item=item,
            defaults={
                "system_quantity": stock.quantity if stock else 0,
                "counted_quantity": float(request.data.get("counted_quantity", 0)),
            },
        )
        return Response(CycleCountLineSerializer(line).data)

    @action(detail=True, methods=["post"])
    def post_count(self, request, pk=None):
        """Apply the count's variances to on-hand stock."""
        from .operations import post_cycle_count
        count = self.get_object()
        adjusted = post_cycle_count(count, user=request.user)
        log_activity(
            request.user, "Inventory", "Cycle Count Posted",
            f"Posted cycle count #{count.id} @ '{count.warehouse.name}': {adjusted} adjustment(s)",
        )
        return Response({"status": "posted", "adjustments": adjusted})


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


class QuickBooksOnboardingViewSet(viewsets.ViewSet):
    """Manage QB onboarding workflow."""
    permission_classes = [IsAdmin]

    @action(detail=False, methods=['get'])
    def status(self, request):
        """Get onboarding status for user's company."""
        company = request.user.company
        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        return Response(QuickBooksOnboardingSerializer(onboarding).data)

    @action(detail=False, methods=['get'])
    def items_to_classify(self, request):
        """Get QB items that need classification."""
        company = request.user.company
        items = Item.objects.filter(company=company, quickbooks_id__isnull=False, erp_classification__isnull=True)
        return Response(ItemSerializer(items, many=True).data)

    @action(detail=False, methods=['post'])
    def acknowledge_disclaimer(self, request):
        """Mark disclaimer as acknowledged."""
        company = request.user.company
        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        onboarding.disclaimer_acknowledged = True
        onboarding.save()
        return Response({'status': 'disclaimer acknowledged'})

    @action(detail=False, methods=['post'])
    def classify_items(self, request):
        """Bulk classify items. Payload: [{'item_id': 1, 'classification': 'raw_material'}, ...]"""
        from django.utils import timezone
        company = request.user.company
        classifications = request.data.get('classifications', [])

        updated_count = 0
        for cls in classifications:
            item_id = cls.get('item_id')
            classification = cls.get('classification')
            if classification not in ['raw_material', 'finished_good', 'out_of_scope']:
                continue

            try:
                item = Item.objects.get(id=item_id, company=company)
                item.erp_classification = classification
                item.classification_completed_at = timezone.now()
                # Keep `category` (used by the rest of the ERP) in sync with the
                # onboarding decision. `category` has no "out_of_scope" value, so
                # out-of-scope items keep their category but are excluded from ERP
                # flows via `erp_classification`.
                if classification in ("raw_material", "finished_good"):
                    item.category = classification
                item.save()
                updated_count += 1
            except Item.DoesNotExist:
                pass

        # Check if all items classified
        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        unclassified = Item.objects.filter(company=company, quickbooks_id__isnull=False, erp_classification__isnull=True).count()

        if unclassified == 0:
            onboarding.status = 'bom_setup'
            onboarding.save()

        return Response({'updated': updated_count, 'next_status': onboarding.status})

    @action(detail=False, methods=['post'])
    def complete_bom_setup(self, request):
        """Finish the BOM phase and advance to customer mapping."""
        company = request.user.company
        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        onboarding.status = 'customer_mapping'
        onboarding.save()
        return Response({'status': 'ok', 'next_status': onboarding.status})

    @action(detail=False, methods=['get', 'post'])
    def sales_config(self, request):
        """Read or update the per-company sales→QuickBooks configuration.

        GET  → current config (created with defaults if absent).
        POST → patch any of: customers_mapped, sale_trigger, default_doc_type,
               price_disclaimer_acknowledged.
        """
        company = request.user.company
        config, _ = SalesQuickBooksConfig.objects.get_or_create(company=company)

        if request.method == 'POST':
            serializer = SalesQuickBooksConfigSerializer(
                config, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        return Response(SalesQuickBooksConfigSerializer(config).data)

    @action(detail=False, methods=['post'])
    def confirm_customer_mapping(self, request):
        """Mark customer mapping reviewed and advance to sales config phase."""
        company = request.user.company
        config, _ = SalesQuickBooksConfig.objects.get_or_create(company=company)
        config.customers_mapped = True
        config.save(update_fields=['customers_mapped', 'updated_at'])

        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        onboarding.status = 'sales_config'
        onboarding.save()
        return Response({'status': 'ok', 'next_status': onboarding.status})

    @action(detail=False, methods=['post'])
    def complete_sales_config(self, request):
        """Finish the sales config phase and advance to vendor mapping."""
        company = request.user.company
        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        onboarding.status = 'vendor_mapping'
        onboarding.save()
        return Response({'status': 'ok', 'next_status': onboarding.status})

    @action(detail=False, methods=['get', 'post'])
    def procurement_config(self, request):
        """Read or update the per-company procurement→QuickBooks configuration.

        GET  → current config (created with defaults if absent).
        POST → patch any of: vendors_mapped, purchase_trigger, cost_source,
               payables_disclaimer_acknowledged.
        """
        company = request.user.company
        config, _ = ProcurementQuickBooksConfig.objects.get_or_create(company=company)

        if request.method == 'POST':
            serializer = ProcurementQuickBooksConfigSerializer(
                config, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        return Response(ProcurementQuickBooksConfigSerializer(config).data)

    @action(detail=False, methods=['post'])
    def confirm_vendor_mapping(self, request):
        """Mark vendor mapping reviewed and advance to procurement config phase."""
        company = request.user.company
        config, _ = ProcurementQuickBooksConfig.objects.get_or_create(company=company)
        config.vendors_mapped = True
        config.save(update_fields=['vendors_mapped', 'updated_at'])

        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        onboarding.status = 'procurement_config'
        onboarding.save()
        return Response({'status': 'ok', 'next_status': onboarding.status})

    @action(detail=False, methods=['post'])
    def mark_onboarding_complete(self, request):
        """Mark onboarding as complete (final step after procurement config)."""
        from django.utils import timezone
        company = request.user.company
        onboarding, _ = QuickBooksOnboarding.objects.get_or_create(company=company)
        onboarding.status = 'completed'
        onboarding.completed_at = timezone.now()
        onboarding.save()
        return Response({'status': 'onboarding completed'})


def _sync_bom_to_recipe(bom):
    """Mirror an onboarding BOM into a Production Recipe.

    Production uses production.Recipe / RecipeIngredient, while onboarding builds
    inventory.BOM / BOMLine. Keeping them in sync means BOMs defined during
    onboarding immediately drive production planning. The BOM stays the source
    of truth; the Recipe is regenerated from it on every change.
    """
    from production.models import Recipe, RecipeIngredient

    recipe, _ = Recipe.objects.get_or_create(product=bom.finished_good)
    # Rebuild ingredients from the current BOM lines.
    RecipeIngredient.objects.filter(recipe=recipe).delete()
    for line in bom.lines.all():
        RecipeIngredient.objects.create(
            recipe=recipe, item=line.raw_material, quantity=line.quantity
        )
    return recipe


class BOMViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Manage Bill of Materials for finished goods."""
    company_field = "finished_good__company"
    queryset = BOM.objects.select_related('finished_good').prefetch_related('lines__raw_material')
    serializer_class = BOMSerializer
    permission_classes = [IsStore | IsAdmin]

    def perform_create(self, serializer):
        bom = serializer.save()
        log_activity(
            self.request.user, "Inventory",
            "Create BOM", f"Created BOM for {bom.finished_good.name}"
        )

    @action(detail=True, methods=['post'])
    def add_line(self, request, pk=None):
        """Add a line item to BOM."""
        bom = self.get_object()
        raw_material_id = request.data.get('raw_material_id')
        quantity = request.data.get('quantity')
        unit = request.data.get('unit', 'unit')

        if not raw_material_id or not quantity:
            return Response({'error': 'raw_material_id and quantity required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            raw_material = Item.objects.get(id=raw_material_id, category='raw_material')
            line, created = BOMLine.objects.get_or_create(
                bom=bom, raw_material=raw_material,
                defaults={'quantity': quantity, 'unit': unit}
            )
            if not created:
                line.quantity = quantity
                line.unit = unit
                line.save()

            bom.finished_good.bom_completed = True
            bom.finished_good.save()

            # Keep the Production recipe in step with the BOM.
            _sync_bom_to_recipe(bom)

            return Response(BOMLineSerializer(line).data, status=status.HTTP_201_CREATED)
        except Item.DoesNotExist:
            return Response({'error': 'Raw material not found or not a raw material'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['delete'])
    def remove_line(self, request, pk=None):
        """Remove a line item from BOM."""
        bom = self.get_object()
        line_id = request.query_params.get('line_id')

        try:
            line = BOMLine.objects.get(id=line_id, bom=bom)
            line.delete()
            # Keep the Production recipe in step with the BOM.
            _sync_bom_to_recipe(bom)
            return Response({'status': 'line removed'})
        except BOMLine.DoesNotExist:
            return Response({'error': 'Line not found'}, status=status.HTTP_404_NOT_FOUND)
