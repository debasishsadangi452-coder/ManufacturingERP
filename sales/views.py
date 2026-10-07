from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from django.db.models import Sum
from django.utils import timezone
from accounts.permission import IsSales, IsAdmin, IsStore, IsFinance, IsProduction, IsQuality

from .models import Customer, CustomerPayment, Invoice, InvoiceLine, SalesOrder, SalesOrderItem, Shipment
from .serializers import *

from inventory.services import decrease_stock
from inventory.models import Stock, Item, Warehouse
from production.models import ProductionOrder, Recipe
from core.utils import send_notification, log_activity
from core.tenancy import CompanyScopedMixin


def approved_finished_goods_available(item, company, order=None):
    """Sellable FG for `order`: QA-approved stock (FG stock only grows on QA
    acceptance) minus what is allocated to other customer orders."""
    from fulfillment.services import available_for_order
    return available_for_order(item, company, order)


class CustomerViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [IsSales | IsAdmin]

class SalesOrderViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "customer__company"
    queryset = SalesOrder.objects.all().order_by('-created_at')
    serializer_class = SalesOrderSerializer
    # Production is included because the Operations Manager builds the
    # production schedule from confirmed customer orders — the orders are the
    # input to that job, so the role needs to see them.
    permission_classes = [IsSales | IsAdmin | IsStore | IsProduction]

    def perform_create(self, serializer):
        order = serializer.save()
        items_summary = ', '.join([f"{i.quantity} x {i.item.name}" for i in order.salesorderitem_set.all()])
        log_activity(self.request.user, "Sales", "Create Sales Order", f"Created SO #{order.id} for customer '{order.customer.name}': {items_summary}")
        
        # Check stock for each item and notify if production/materials needed
        for order_item in order.salesorderitem_set.all():
            item = order_item.item
            requested_qty = order_item.quantity
            
            available_qty = approved_finished_goods_available(item, order.customer.company)
            
            if available_qty < requested_qty:
                shortage = requested_qty - available_qty
                
                # Verify item has a recipe (cannot produce without one)
                recipe = Recipe.objects.filter(product=item).first()
                if not recipe:
                    send_notification(
                        "admin",
                        f"CRITICAL: SO#{order.id} needs {item.name}, but NO RECIPE is defined!",
                        related_id=order.id,
                        related_type="sales_order",
                        company=order.customer.company,
                        module="sales",
                    )
                    continue

                # Notify Production to start a new batch
                send_notification(
                    "production",
                    f"FULFILLMENT REQ: Produce {shortage} {item.unit} of {item.name} for SO#{order.id}",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company,
                    module="production",
                )
                
                # Notify Store (Inventory) to gather raw materials for this production
                send_notification(
                    "store",
                    f"GATHERING REQ: Prepare raw materials for production of {item.name} (SO#{order.id})",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company,
                    module="inventory",
                )

    @action(detail=True, methods=['post'])
    def mark_ready_for_production(self, request, pk=None):
        """
        Called by Store/Inventory user when they confirm materials are being prepared.
        This action:
          1. Checks that all required raw materials are available in inventory.
          2. Deducts (reserves) those raw materials from inventory stock.
          3. Creates a ProductionOrder for each finished good needed.
          4. Notifies the Production team.
          5. Sets the Sales Order status to 'confirmed'.
        """
        order = self.get_object()

        if order.status != 'pending':
            return Response(
                {"error": f"Order is already '{order.status}'. Only pending orders can be prepared."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Production must happen in a warehouse belonging to THIS order's
        # company — an unscoped lookup will happily pick another tenant's.
        warehouse = Warehouse.objects.filter(company=order.customer.company).first()
        if not warehouse:
            return Response(
                {"error": f"No warehouse configured for {order.customer.company}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        errors = []
        production_orders_created = []

        from fulfillment.services import allocate
        from production.execution import create_production_order, reserve_materials

        for order_item in order.salesorderitem_set.all():
            item = order_item.item
            qty_needed = order_item.quantity

            # MTO: QA-approved stock on hand is allocated to this order first so
            # no other order can take it; only the remainder is produced.
            available_sellable = approved_finished_goods_available(item, order.customer.company, order)
            if available_sellable > 0:
                allocate(order, request.user, [{
                    "order_item_id": order_item.id,
                    "quantity": min(available_sellable, qty_needed),
                }])

            if available_sellable >= qty_needed:
                send_notification(
                    "sales",
                    f"STOCK ALLOCATED: SO#{order.id} for {item.name} is covered by existing stock.",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company,
                    module="sales",
                )
                continue

            # --- Step 1: Find recipe (Only if we need to produce) ---
            recipe = Recipe.objects.filter(product=item).first()
            if not recipe:
                errors.append(f"No recipe defined for '{item.name}'. Cannot plan production.")
                continue

            remaining_to_produce = qty_needed - available_sellable

            # --- Step 2: Create the production order (TB-01 conversion). ---
            # The creation signal raises inventory requests for the true
            # shortage; available material is then issued onto the ledger, so
            # completion never deducts it a second time.
            try:
                from production.models import ProductionPlan

                with transaction.atomic():
                    plan = ProductionPlan.objects.filter(
                        sales_order=order, sales_order_item=order_item,
                    ).exclude(status="cancelled").first()
                    if plan is None:
                        plan = ProductionPlan.objects.create(
                            company=order.customer.company if order.customer else None,
                            sales_order=order,
                            sales_order_item=order_item,
                            customer=order.customer,
                            item=item,
                            order_quantity=qty_needed,
                            planned_quantity=remaining_to_produce,
                            target_date=order.required_delivery_date,
                            status="planned",
                            notes=f"Auto-planned via inventory preparation for SO#{order.id}",
                            created_by=request.user if request.user.is_authenticated else None,
                        )
                    prod_order, _checks = create_production_order(
                        recipe, remaining_to_produce, warehouse, sales_order=order,
                        plan_ref=plan.plan_number, status="scheduled",
                    )
                    prod_order.production_plan = plan
                    prod_order.save(update_fields=["production_plan"])
                    if plan.status != "converted":
                        plan.status = "converted"
                        plan.save(update_fields=["status", "updated_at"])
            except ValidationError as e:
                detail = e.detail.get("conversion", e.detail) if isinstance(e.detail, dict) else e.detail
                errors.append(f"'{item.name}': " + " ".join(str(d) for d in (detail if isinstance(detail, list) else [detail])))
                continue
            production_orders_created.append(prod_order.id)

            # --- Step 3: Issue what IS on hand (never more than available). ---
            shortage_rows = reserve_materials(prod_order, request.user, f"Reserved for SO#{order.id} production")
            fully_reserved = not shortage_rows
            if not fully_reserved:
                prod_order.status = "material_pending"
                prod_order.save(update_fields=["status"])
                shortages = [
                    f"{it.name} (short {qty:.0f}"
                    + (" - intermediate, produce it first" if it.category == "intermediate" else "") + ")"
                    for it, qty in shortage_rows
                ]
                errors.append(
                    f"'{item.name}': awaiting materials — {', '.join(shortages)}. "
                    f"Inventory requests raised."
                )
                send_notification(
                    "store",
                    f"MATERIAL SHORTAGE for SO#{order.id} ({item.name}): {', '.join(shortages)}. "
                    f"Send to procurement.",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company,
                    module="inventory",
                )

            # --- Step 4: Notify Production ---
            send_notification(
                "production",
                f"NEW BATCH: Produce {remaining_to_produce} {item.unit} of {item.name} for SO#{order.id} ({prod_order.order_number}). "
                + ("Materials reserved." if fully_reserved else "AWAITING MATERIALS."),
                related_id=prod_order.id,
                related_type="production_order",
                company=order.customer.company,
                module="production",
            )

        # Only a hard failure (no recipe at all) blocks the order now — a material
        # shortage raises inventory requests and still schedules the batch.
        if errors and not production_orders_created:
            return Response({"error": "Could not prepare order.", "details": errors}, status=status.HTTP_400_BAD_REQUEST)

        # Mark the order as confirmed (materials prepped OR stock allocated)
        order.status = 'confirmed'
        order.save()

        msg = f"SO#{order.id} confirmed."
        if production_orders_created:
            msg += f" Production orders {production_orders_created} created."
        else:
            msg += " All items allocated from existing stock."
            
        if errors:
            msg += f" Warnings: {errors}"

        send_notification(
            "sales",
            f"SO#{order.id} is confirmed. {'Existing stock allocated.' if not production_orders_created else 'Production has been notified and raw materials are reserved.'}",
            related_id=order.id,
            related_type="sales_order",
            company=order.customer.company,
            module="sales",
        )

        log_activity(request.user, "Sales", "Mark Ready for Production", f"SO #{order.id} marked ready. POs created: {production_orders_created}. Warnings: {errors or 'None'}")
        return Response({
            "status": "confirmed",
            "message": msg,
            "production_orders": production_orders_created,
            "warnings": errors
        })

    @action(detail=True, methods=['post'], permission_classes=[IsSales | IsProduction | IsAdmin | IsStore])
    def create_production_plan(self, request, pk=None):
        """
        Creates Production Plan Order(s) from a Sales Order.
        If item_id is provided, creates a plan for that specific order line.
        Otherwise, creates plans for all order lines that do not have active plans yet.
        """
        order = self.get_object()

        if order.status in ['cancelled', 'delivered']:
            return Response(
                {"error": f"Cannot create production plan for an order with status '{order.status}'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        from production.models import ProductionPlan
        from production.serializers import ProductionPlanSerializer

        item_id = request.data.get('item_id')
        target_date = request.data.get('target_date') or order.required_delivery_date or None
        notes = request.data.get('notes', '')

        lines_to_plan = order.salesorderitem_set.all()
        if item_id:
            lines_to_plan = lines_to_plan.filter(item_id=item_id)
            if not lines_to_plan.exists():
                return Response(
                    {"error": f"Item {item_id} not found in this sales order."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        if not lines_to_plan.exists():
            return Response(
                {"error": "No order items found to create a production plan for."},
                status=status.HTTP_400_BAD_REQUEST
            )

        created_plans = []
        for line in lines_to_plan:
            existing_plan = ProductionPlan.objects.filter(
                sales_order=order,
                sales_order_item=line,
            ).exclude(status='cancelled').first()

            if existing_plan:
                if item_id:
                    return Response(
                        {"error": f"Production Plan {existing_plan.plan_number} already exists for item '{line.item.name}'."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                continue

            planned_qty = request.data.get('planned_quantity')
            if planned_qty is not None and item_id:
                try:
                    planned_qty = float(planned_qty)
                    if planned_qty <= 0:
                        return Response(
                            {"error": "Planned quantity must be greater than zero."},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except (ValueError, TypeError):
                    return Response(
                        {"error": "Invalid planned quantity format."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                planned_qty = line.quantity

            plan = ProductionPlan.objects.create(
                company=order.customer.company if order.customer else None,
                sales_order=order,
                sales_order_item=line,
                customer=order.customer,
                item=line.item,
                order_quantity=line.quantity,
                planned_quantity=planned_qty,
                target_date=target_date,
                status="planned",
                notes=notes,
                created_by=request.user if request.user.is_authenticated else None,
            )
            created_plans.append(plan)

        if not created_plans:
            return Response(
                {"error": "All items in this order already have active production plans."},
                status=status.HTTP_400_BAD_REQUEST
            )

        log_activity(
            request.user,
            "Sales",
            "Create Production Plan",
            f"Created {len(created_plans)} production plans for SO #{order.id}"
        )

        return Response(
            {
                "message": f"Successfully created {len(created_plans)} production plan(s).",
                "plans": ProductionPlanSerializer(created_plans, many=True).data,
            },
            status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=['get'])

    def production_requests(self, request):
        """Orders Inventory has sent to Production.

        This is the feed for Production's Requests tab: one row per sales order
        that Inventory confirmed, excluding any whose batches are all finished
        (those have moved on to Inventory's "Send to Shipment" step).
        """
        orders = self.filter_queryset(self.get_queryset()).filter(status='confirmed')

        results = []
        for order in orders:
            batches = list(order.production_orders.all())
            # Confirmed but no batch = fully covered by stock; nothing to produce.
            if not batches:
                continue
            pending = [b for b in batches if b.status != 'completed']
            if not pending:
                continue

            results.append({
                "sales_order_id": order.id,
                "customer_name": order.customer.name,
                "created_at": order.created_at,
                "items": [
                    {"item_name": oi.item.name, "quantity": oi.quantity, "unit": oi.item.unit}
                    for oi in order.salesorderitem_set.all()
                ],
                "production_orders": [
                    {
                        "id": b.id,
                        "product": b.recipe.product.name,
                        "quantity": b.quantity,
                        "status": b.status,
                        "materials_reserved": b.materials_reserved,
                        "production_plan_id": b.production_plan_id,
                        "production_plan_number": b.production_plan.plan_number if b.production_plan else None,
                    }
                    for b in batches
                ],
                # Convenience for the tab's primary button
                "next_batch_id": pending[0].id,
                "all_running": all(b.status == 'running' for b in pending),
            })

        return Response(results)

    @action(detail=False, methods=['get'])
    def available_inventory(self, request):
        """
        List items that are Finished Goods, have a Recipe, and have passed quality checks.
        """
        company = request.user.company
        items = Item.objects.filter(
            category='finished_good', recipes__isnull=False, company=company
        ).exclude(erp_classification='out_of_scope').distinct()

        results = []
        for item in items:
            # Physical finished-goods stock in THIS company's warehouses — the
            # same basis fulfil_order uses, so the tab never contradicts it.
            physical_stock = Stock.objects.filter(
                item=item, warehouse__company=company
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0

            results.append({
                "id": item.id,
                "name": item.name,
                "total_stock": physical_stock,
                "available_for_sales": approved_finished_goods_available(item, company),
                "unit": item.unit
            })
            
        return Response(results)

    @action(detail=True, methods=['post'])
    def fulfill_order(self, request, pk=None):
        """
        Called by the Sales user to complete and ship an order.
        Prerequisites: order must be 'confirmed' and quality check approved.
        This action:
          1. Checks finished goods are in stock (produced & quality-approved).
          2. Deducts the finished goods from inventory.
          3. Marks the order as 'delivered'.
          4. Notifies relevant users.
        """
        order = self.get_object()

        if order.status not in ('confirmed', 'shipped'):
            return Response(
                {"error": f"Order is '{order.status}'. Only confirmed orders can be fulfilled."},
                status=status.HTTP_400_BAD_REQUEST
            )

        company = order.customer.company
        # Pick a warehouse belonging to this company for stock deduction
        warehouse = Warehouse.objects.filter(company=company).first()
        if not warehouse:
            return Response({"error": "No warehouse configured."}, status=status.HTTP_400_BAD_REQUEST)

        errors = []
        for order_item in order.salesorderitem_set.all():
            item = order_item.item
            qty_needed = order_item.quantity - order_item.shipped_quantity

            if qty_needed <= 0:
                continue

            available = approved_finished_goods_available(item, company, order)

            if available < qty_needed:
                errors.append(
                    f"'{item.name}': need {qty_needed}, only {available} in stock."
                )

        if errors:
            return Response({
                "error": "Insufficient finished-goods stock to fulfill order.",
                "details": errors
            }, status=status.HTTP_400_BAD_REQUEST)

        # Deduct finished goods from inventory
        shipped_lines = []
        for order_item in order.salesorderitem_set.all():
            qty_needed = order_item.quantity - order_item.shipped_quantity
            if qty_needed <= 0:
                continue

            stock_entry = Stock.objects.filter(item=order_item.item, warehouse__company=company).order_by('-quantity').first()
            if stock_entry:
                try:
                    decrease_stock(
                        order_item.item,
                        stock_entry.warehouse,
                        qty_needed,
                        user=request.user,
                        reference=f"Fulfilled SO#{order.id}"
                    )
                    order_item.shipped_quantity += qty_needed
                    order_item.save()
                    from fulfillment.services import consume_allocations
                    consume_allocations(order_item, qty_needed)
                    shipped_lines.append({"item_id": order_item.item_id, "quantity": qty_needed})
                except ValueError as e:
                    return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Mark order as delivered
        order.status = 'delivered'
        order.save()

        # Create Logistics Shipment
        Shipment.objects.create(
            sales_order=order,
            warehouse=warehouse,
            status="in-transit",
            progress=50,
            driver="Full FTL Route"
        )

        send_notification(
            "store",
            f"SO#{order.id} FULFILLED. {', '.join([f'{oi.quantity} {oi.item.unit} of {oi.item.name}' for oi in order.salesorderitem_set.all()])} deducted from inventory.",
            related_id=order.id,
            related_type="sales_order",
            company=order.customer.company,
            module="inventory",
        )

        from accounting.auto_posting import queue_auto_post
        queue_auto_post("sales_shipment", company, order.id, request.user, {"lines": shipped_lines})

        log_activity(request.user, "Sales", "Fulfill Sales Order", f"SO #{order.id} fulfilled and delivered. Items: {', '.join([f'{oi.quantity} x {oi.item.name}' for oi in order.salesorderitem_set.all()])}")
        return Response({"status": "delivered", "message": f"SO#{order.id} fulfilled and marked as delivered."})

    @action(detail=True, methods=['post'])
    def partial_fulfill(self, request, pk=None):
        """
        Partial fulfillment: ship whatever quantity is specified per item.
        Payload: { "items": [{ "order_item_id": 1, "ship_qty": 5 }, ...] }
        - Deducts the ship_qty for each item from finished goods stock.
        - If all items are fully shipped → status = 'delivered'.
        - If some items are partially shipped → status = 'shipped' (partial).
        - Remaining quantities trigger a notification to production.
        """
        order = self.get_object()

        if order.status not in ('confirmed', 'shipped'):
            return Response(
                {"error": f"Order is '{order.status}'. Only confirmed/shipped orders can be partially fulfilled."},
                status=status.HTTP_400_BAD_REQUEST
            )

        ship_map = {
            int(i['order_item_id']): float(i['ship_qty'])
            for i in request.data.get('items', [])
            if float(i.get('ship_qty', 0)) > 0
        }

        if not ship_map:
            return Response({"error": "No items with a valid ship quantity provided."}, status=status.HTTP_400_BAD_REQUEST)

        shipped_summary = []
        shipped_lines = []
        remaining_summary = []
        fully_fulfilled = True

        for order_item in order.salesorderitem_set.all():
            ship_qty = ship_map.get(order_item.id, 0)
            ordered_qty = order_item.quantity - order_item.shipped_quantity

            if ship_qty <= 0:
                # Skipped entirely — counts as not yet shipped
                remaining = ordered_qty
                if remaining > 0:
                    fully_fulfilled = False
                    remaining_summary.append(f"{ordered_qty} {order_item.item.unit} of {order_item.item.name}")
                continue

            if ship_qty > ordered_qty:
                return Response(
                    {"error": f"Cannot ship {ship_qty} of '{order_item.item.name}' — only {ordered_qty} remaining to ship."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            available = approved_finished_goods_available(order_item.item, order.customer.company, order)
            if available < ship_qty:
                return Response(
                    {"error": f"Insufficient QA-approved stock for '{order_item.item.name}': need {ship_qty}, have {available}."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Deduct from inventory
            stock_entry = Stock.objects.filter(
                item=order_item.item, warehouse__company=order.customer.company
            ).order_by('-quantity').first()
            if stock_entry:
                try:
                    decrease_stock(
                        order_item.item,
                        stock_entry.warehouse,
                        ship_qty,
                        user=request.user,
                        reference=f"Partial fulfillment SO#{order.id}"
                    )
                except ValueError as e:
                    return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

                order_item.shipped_quantity += ship_qty
                order_item.save()
                from fulfillment.services import consume_allocations
                consume_allocations(order_item, ship_qty)
                shipped_lines.append({"item_id": order_item.item_id, "quantity": ship_qty})

            shipped_summary.append(f"{ship_qty} {order_item.item.unit} of {order_item.item.name}")

            remaining = ordered_qty - ship_qty
            if remaining > 0:
                fully_fulfilled = False
                remaining_summary.append(f"{remaining} {order_item.item.unit} of {order_item.item.name}")
                # Notify production about the remaining gap
                send_notification(
                    "production",
                    f"PARTIAL SHIP: SO#{order.id} still needs {remaining} {order_item.item.unit} of {order_item.item.name}. Please fulfil remaining batch.",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company,
                    module="production",
                )

        # Update order status
        order.status = 'delivered' if fully_fulfilled else 'shipped'
        order.save()

        # Create Logistics Shipment for this partial drop — from this
        # company's own warehouse, never another tenant's.
        warehouse = Warehouse.objects.filter(company=order.customer.company).first()
        if warehouse:
            Shipment.objects.create(
                sales_order=order,
                warehouse=warehouse,
                status="in-transit",
                progress=20,
                driver="Partial LTL Route"
            )

        from accounting.auto_posting import queue_auto_post
        queue_auto_post("sales_shipment", order.customer.company, order.id, request.user, {"lines": shipped_lines})

        send_notification(
            "store",
            f"Partial shipment: SO#{order.id} — Shipped: {', '.join(shipped_summary)}. Remaining: {', '.join(remaining_summary) or 'None'}.",
            related_id=order.id,
            related_type="sales_order",
            company=order.customer.company,
            module="inventory",
        )

        return Response({
            "status": order.status,
            "shipped": shipped_summary,
            "remaining": remaining_summary,
            "message": (
                f"Order fully delivered." if fully_fulfilled
                else f"Partial shipment sent. Remaining items triggered production notification."
            )
        })

    @action(detail=True, methods=['post'])
    def generate_invoice(self, request, pk=None):
        """
        Create an Invoice from this sales order using the order line agreed price.
        The invoice is mirrored to QuickBooks automatically.
        Optional payload: { "due_date": "YYYY-MM-DD" }
        """
        order = self.get_object()

        if order.status == 'cancelled':
            return Response({"error": "Cannot invoice a cancelled order."}, status=status.HTTP_400_BAD_REQUEST)
        # Invoices whose lines are not tied to order lines bill the whole order.
        existing = order.invoices.exclude(status='cancelled').exclude(
            lines__sales_order_item__isnull=False
        ).first()
        if existing:
            return Response(
                {"error": f"Invoice INV-{existing.id} already exists for this order."},
                status=status.HTTP_400_BAD_REQUEST
            )
        from fulfillment.services import invoiced_quantity
        billable = {}
        for order_item in order.salesorderitem_set.all():
            basis = order_item.shipped_quantity or order_item.quantity
            remaining = basis - invoiced_quantity(order_item)
            if remaining > 0:
                billable[order_item.id] = remaining
        if not billable:
            return Response(
                {"error": "Everything on this order has already been invoiced."},
                status=status.HTTP_400_BAD_REQUEST
            )

        due_date = None
        if request.data.get('due_date'):
            try:
                due_date = date.fromisoformat(str(request.data['due_date']))
            except ValueError:
                return Response({"error": "due_date must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        if due_date is None:
            due_date = timezone.localdate() + timedelta(days=30)

        invoice = Invoice.objects.create(
            company=order.customer.company,
            sales_order=order,
            customer=order.customer,
            due_date=due_date,
        )
        total = Decimal("0")
        for order_item in order.salesorderitem_set.select_related('item'):
            if order_item.id not in billable:
                continue
            # The agreed order price; the item master price only for legacy
            # lines entered before prices were captured on the order.
            unit_price = order_item.unit_price or order_item.item.selling_price or Decimal("0")
            invoice_qty = billable[order_item.id]
            amount = unit_price * Decimal(str(invoice_qty))
            InvoiceLine.objects.create(
                invoice=invoice,
                sales_order_item=order_item,
                item=order_item.item,
                description=order_item.item.name,
                quantity=invoice_qty,
                unit_price=unit_price,
                amount=amount,
            )
            total += amount
        invoice.total_amount = total or order.total_amount
        invoice.save(update_fields=["total_amount"])

        # Mirror to QuickBooks (Level 1 sync); failures land in sync errors.
        from quickbooks.push import get_active_connection, safe_push
        connection = get_active_connection(order.customer.company)
        if connection:
            safe_push(connection, "invoice", invoice)

        from accounting.auto_posting import queue_auto_post
        queue_auto_post("sales_invoice", invoice.company, invoice.id, request.user)

        log_activity(request.user, "Sales", "Generate Invoice", f"Generated INV-{invoice.id} for SO #{order.id} (total: {invoice.total_amount})")
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


class InvoiceViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    company_field = "company"
    queryset = Invoice.objects.select_related("customer", "sales_order").prefetch_related("lines__item")
    serializer_class = InvoiceSerializer
    permission_classes = [IsSales | IsAdmin | IsFinance]

    @action(detail=True, methods=['post'])
    def record_payment(self, request, pk=None):
        """
        Record a customer payment against this invoice and mirror it to
        QuickBooks (which marks the invoice paid there too).
        Payload: { "amount": 50000, "method": "bank_transfer", "reference": "...", "payment_date": "YYYY-MM-DD" }
        """
        invoice = self.get_object()
        if invoice.status in ('paid', 'cancelled'):
            return Response({"error": f"Invoice is already {invoice.status}."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            amount = Decimal(str(request.data.get('amount', '0')))
        except InvalidOperation:
            return Response({"error": "amount must be a number."}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"error": "amount must be greater than zero."}, status=status.HTTP_400_BAD_REQUEST)
        if amount > invoice.balance_due:
            return Response(
                {"error": f"amount exceeds balance due ({invoice.balance_due})."},
                status=status.HTTP_400_BAD_REQUEST
            )

        payment_date = timezone.localdate()
        if request.data.get('payment_date'):
            try:
                payment_date = date.fromisoformat(str(request.data['payment_date']))
            except ValueError:
                return Response({"error": "payment_date must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        payment = CustomerPayment.objects.create(
            company=invoice.company,
            customer=invoice.customer,
            invoice=invoice,
            amount=amount,
            payment_date=payment_date,
            method=request.data.get('method', 'bank_transfer'),
            reference=request.data.get('reference', ''),
        )
        invoice.apply_payment(amount)
        # Mirrored to QuickBooks by the CustomerPayment post_save signal.

        from accounting.auto_posting import queue_auto_post
        queue_auto_post("customer_payment", invoice.company, payment.id, request.user)

        log_activity(request.user, "Sales", "Record Payment", f"Recorded payment of {amount} against INV-{invoice.id} ({invoice.status})")
        return Response({
            "payment": CustomerPaymentSerializer(payment).data,
            "invoice": InvoiceSerializer(invoice).data,
        }, status=status.HTTP_201_CREATED)


class CustomerPaymentViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    company_field = "company"
    queryset = CustomerPayment.objects.select_related("customer", "invoice")
    serializer_class = CustomerPaymentSerializer
    permission_classes = [IsSales | IsAdmin | IsFinance]


class SalesOrderItemViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "sales_order__customer__company"
    queryset = SalesOrderItem.objects.all()
    serializer_class = SalesOrderItemSerializer
    permission_classes = [IsSales | IsAdmin]

class ShipmentViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "sales_order__customer__company"
    queryset = Shipment.objects.all()
    serializer_class = ShipmentSerializer
    # Store dispatches shipments; Quality verifies shipped quantities and lot
    # codes against what left the building (SQF requirement).
    permission_classes = [IsSales | IsAdmin | IsStore | IsQuality]

    def perform_create(self, serializer):

        order = serializer.validated_data["sales_order"]
        company = order.customer.company
        # Only the unshipped balance leaves, and only from QA-approved stock
        # that is free or allocated to this order (Track B QA gate).
        to_ship = [
            (oi, oi.quantity - oi.shipped_quantity)
            for oi in order.salesorderitem_set.all() if oi.quantity - oi.shipped_quantity > 0
        ]
        for oi, qty in to_ship:
            available = approved_finished_goods_available(oi.item, company, order)
            if available < qty:
                raise ValidationError({
                    "error": f"Insufficient QA-approved stock for '{oi.item.name}': need {qty}, have {available}."
                })
        shipment = serializer.save()

        # Deduct finished goods stock on shipment
        from inventory.lots import ship_lots_fifo
        from fulfillment.services import consume_allocations
        for item, qty in to_ship:
            decrease_stock(
                item.item,
                shipment.warehouse,
                qty,
                user=self.request.user,
                reference=f"Shipment SO#{order.id}"
            )
            # 🔗 SQF traceability: record which finished lots left on this
            # shipment (FIFO), closing the receive→produce→QC→ship chain.
            ship_lots_fifo(
                shipment, item.item, qty,
                company=getattr(item.item, "company", None),
            )
            item.shipped_quantity += qty
            item.save(update_fields=["shipped_quantity"])
            consume_allocations(item, qty)

        # Update order status
        order.status = "shipped"
        order.save()

        from accounting.auto_posting import queue_auto_post
        queue_auto_post(
            "sales_shipment", company, order.id, self.request.user,
            {"lines": [{"item_id": i.item_id, "quantity": q} for i, q in to_ship]},
        )


class InboundOrderEmailViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """The 'Orders from Email' inbox. List/detail plus a `confirm` action that
    promotes a reviewed draft into a real order, and delete for clearing
    unwanted mail.

    Confirming flips the linked SalesOrder from "draft" → "pending", which
    releases the QuickBooks push guardrail so the order syncs normally from
    that point on. Nothing syncs while it sits in draft."""
    company_field = "company"
    queryset = InboundOrderEmail.objects.select_related("sales_order").all()
    serializer_class = InboundOrderEmailSerializer
    permission_classes = [IsSales | IsStore | IsAdmin]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def perform_destroy(self, instance):
        """Delete an inbox entry. Confirmed mail is the audit record of an order
        that reached production and QuickBooks, so it is never deletable —
        removing it would break the order's provenance trail (SQF)."""
        if instance.status == "confirmed":
            raise ValidationError(
                {"error": "Confirmed emails cannot be deleted; they are the order's audit record."}
            )
        log_activity(
            self.request.user, "Sales", "Delete Inbound Email",
            f"Deleted inbound order email #{instance.id} from '{instance.sender}'",
        )
        instance.delete()

    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Delete several inbox entries. Body: {"ids": [...]} or
        {"all_unconfirmed": true}. Confirmed mail is always kept and reported
        back rather than silently skipped."""
        qs = self.get_queryset()

        if request.data.get("all_unconfirmed"):
            targets = list(qs.exclude(status="confirmed"))
            protected = []
        else:
            ids = request.data.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return Response(
                    {"error": "Provide a non-empty 'ids' list, or 'all_unconfirmed': true."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            selected = list(qs.filter(id__in=ids))
            targets = [e for e in selected if e.status != "confirmed"]
            protected = [e.id for e in selected if e.status == "confirmed"]

        deleted_ids = [e.id for e in targets]
        if deleted_ids:
            qs.filter(id__in=deleted_ids).delete()
            log_activity(
                request.user, "Sales", "Delete Inbound Emails",
                f"Deleted {len(deleted_ids)} inbound order email(s)",
            )

        return Response({
            "deleted": len(deleted_ids),
            "deleted_ids": deleted_ids,
            "protected_confirmed": protected,
        })

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        inbound = self.get_object()
        order = inbound.sales_order
        if not order:
            return Response(
                {"detail": "No draft order to confirm — parse produced no matched customer/lines."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.status != "draft":
            return Response(
                {"detail": f"Order already confirmed (status: {order.status})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.status = "pending"  # leaving "draft" releases the QB push guardrail
        order.save()
        inbound.status = "confirmed"
        inbound.save(update_fields=["status"])
        log_activity(
            request.user, "Sales", "Confirm Email Order",
            f"Confirmed email order from {inbound.sender} → SO-{order.id}",
        )
        return Response(SalesOrderSerializer(order).data)


class PriceListViewSet(viewsets.ViewSet):
    """Selling prices of finished goods. Sales, Finance and Admin can change the
    price only — the item itself stays managed from Inventory. The price drives
    sales order totals and invoices, and is mirrored to QuickBooks with the item.
    Store users (who can also raise sales orders) may read prices, not change them."""

    def get_permissions(self):
        if self.action == "list":
            return [(IsSales | IsAdmin | IsFinance | IsStore)()]
        return [(IsSales | IsAdmin | IsFinance)()]

    def _items(self, request):
        company = getattr(request.user, "company", None)
        if company is None:
            return Item.objects.none()
        return (
            Item.objects.filter(company=company, category="finished_good")
            .exclude(erp_classification="out_of_scope")
            .order_by("name")
        )

    def _row(self, item):
        from accounting.auto_posting import standard_unit_cost
        cost, _ = standard_unit_cost(item)
        return {
            "id": item.id,
            "name": item.name,
            "sku": item.sku,
            "unit": item.unit,
            "selling_price": str(item.selling_price),
            "standard_cost": str(Decimal(cost).quantize(Decimal("0.01"))),
        }

    def list(self, request):
        return Response([self._row(item) for item in self._items(request)])

    def partial_update(self, request, pk=None):
        item = self._items(request).filter(pk=pk).first()
        if not item:
            return Response({"error": "Finished good not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            price = Decimal(str(request.data.get("selling_price", "")))
        except InvalidOperation:
            return Response({"error": "selling_price must be a number."}, status=status.HTTP_400_BAD_REQUEST)
        if price < 0:
            return Response({"error": "selling_price cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)

        old_price = item.selling_price
        item.selling_price = price.quantize(Decimal("0.01"))
        item.save(update_fields=["selling_price"])
        log_activity(request.user, "Sales", "Update Selling Price", f"'{item.name}' selling price {old_price} → {item.selling_price}")
        return Response(self._row(item))
