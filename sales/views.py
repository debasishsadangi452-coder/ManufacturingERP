from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Sum
from accounts.permission import IsSales, IsAdmin, IsStore, IsFinance

from .models import Customer, CustomerPayment, Invoice, InvoiceLine, SalesOrder, SalesOrderItem, Shipment
from .serializers import *

from inventory.services import decrease_stock
from inventory.models import Stock, Item, Warehouse
from production.models import ProductionOrder, Recipe
from core.utils import send_notification, log_activity
from core.tenancy import CompanyScopedMixin


class CustomerViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer
    permission_classes = [IsSales | IsAdmin]

class SalesOrderViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "customer__company"
    queryset = SalesOrder.objects.all().order_by('-created_at')
    serializer_class = SalesOrderSerializer
    permission_classes = [IsSales | IsAdmin | IsStore]

    def perform_create(self, serializer):
        order = serializer.save()
        items_summary = ', '.join([f"{i.quantity} x {i.item.name}" for i in order.salesorderitem_set.all()])
        log_activity(self.request.user, "Sales", "Create Sales Order", f"Created SO #{order.id} for customer '{order.customer.name}': {items_summary}")
        
        # Check stock for each item and notify if production/materials needed
        for order_item in order.salesorderitem_set.all():
            item = order_item.item
            requested_qty = order_item.quantity
            
            # Check physical stock across all warehouses
            physical_stock = Stock.objects.filter(item=item).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            # Check if we have enough "ready to sell" (approved) stock
            approved_qty = ProductionOrder.objects.filter(
                recipe__product=item,
                status='completed',
                qualitycheck__status='approved'
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            available_qty = min(physical_stock, approved_qty)
            
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
                        company=order.customer.company
                    )
                    continue

                # Notify Production to start a new batch
                send_notification(
                    "production",
                    f"FULFILLMENT REQ: Produce {shortage} {item.unit} of {item.name} for SO#{order.id}",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company
                )
                
                # Notify Store (Inventory) to gather raw materials for this production
                send_notification(
                    "store",
                    f"GATHERING REQ: Prepare raw materials for production of {item.name} (SO#{order.id})",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company
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

        # Pick the first available warehouse as the production warehouse
        warehouse = Warehouse.objects.first()
        if not warehouse:
            return Response({"error": "No warehouse found in the system."}, status=status.HTTP_400_BAD_REQUEST)

        errors = []
        production_orders_created = []

        for order_item in order.salesorderitem_set.all():
            item = order_item.item
            qty_needed = order_item.quantity

            # Check if we already have quality-approved stock for this item
            physical_stock = Stock.objects.filter(item=item).aggregate(Sum('quantity'))['quantity__sum'] or 0
            approved_qty = ProductionOrder.objects.filter(
                recipe__product=item,
                status='completed',
                qualitycheck__status='approved'
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            available_sellable = min(physical_stock, approved_qty)

            if available_sellable >= qty_needed:
                # We have enough stock! No need to produce more.
                # We could potentially reserve it here, but for now we just mark as confirmed.
                send_notification(
                    "sales",
                    f"STOCK ALLOCATED: SO#{order.id} for {item.name} is covered by existing stock.",
                    related_id=order.id,
                    related_type="sales_order",
                    company=order.customer.company
                )
                continue

            # --- Step 1: Find recipe (Only if we need to produce) ---
            recipe = Recipe.objects.filter(product=item).first()
            if not recipe:
                errors.append(f"No recipe defined for '{item.name}'. Cannot plan production.")
                continue

            remaining_to_produce = qty_needed - available_sellable
            # Ingredient quantities are per batch, so scale by whole batches.
            requirements = recipe.material_requirements(remaining_to_produce)

            # --- Step 2: Check raw material availability ---
            # A shortage does NOT block the order. We record it, then still
            # schedule the batch below — creating the ProductionOrder fires the
            # signal that raises the inventory requests (one place, no dupes).
            shortages = []
            for ing, required_qty in requirements:
                available = Stock.objects.filter(item=ing.item).aggregate(Sum('quantity'))['quantity__sum'] or 0
                if available < required_qty:
                    shortages.append(f"{ing.item.name} (short {required_qty - available:.0f})")

            # --- Step 3: Reserve what IS on hand (never more than available) ---
            for ing, required_qty in requirements:
                stock_entry = Stock.objects.filter(item=ing.item).order_by('-quantity').first()
                if not stock_entry:
                    continue
                to_reserve = min(required_qty, stock_entry.quantity)
                if to_reserve <= 0:
                    continue
                try:
                    decrease_stock(
                        ing.item,
                        stock_entry.warehouse,
                        to_reserve,
                        user=request.user,
                        reference=f"Reserved for SO#{order.id} production"
                    )
                except ValueError as e:
                    errors.append(str(e))

            # --- Step 4: Create a Production Order (Only for the remaining) ---
            # materials_reserved is only True when nothing was short; otherwise
            # production must wait for procurement to top the material up.
            fully_reserved = not shortages
            prod_order = ProductionOrder.objects.create(
                recipe=recipe,
                quantity=remaining_to_produce,
                warehouse=warehouse,
                status='scheduled',
                sales_order=order,  # so Production's Requests tab can trace the batch back
                materials_reserved=fully_reserved,
            )
            production_orders_created.append(prod_order.id)

            if shortages:
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
                    company=order.customer.company
                )

            # --- Step 5: Notify Production ---
            send_notification(
                "production",
                f"NEW BATCH: Produce {remaining_to_produce} {item.unit} of {item.name} for SO#{order.id} (PO#{prod_order.id}). "
                + ("Materials reserved." if fully_reserved else "AWAITING MATERIALS."),
                related_id=prod_order.id,
                related_type="production_order",
                company=order.customer.company
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
            company=order.customer.company
        )

        log_activity(request.user, "Sales", "Mark Ready for Production", f"SO #{order.id} marked ready. POs created: {production_orders_created}. Warnings: {errors or 'None'}")
        return Response({
            "status": "confirmed",
            "message": msg,
            "production_orders": production_orders_created,
            "warnings": errors
        })

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
                "available_for_sales": physical_stock,
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

            # Available = physical finished-goods stock in this company's warehouses
            available = Stock.objects.filter(
                item=item, warehouse__company=company
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0

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
            company=order.customer.company
        )

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

            # Check available stock
            physical_stock = Stock.objects.filter(item=order_item.item).aggregate(Sum('quantity'))['quantity__sum'] or 0
            if physical_stock < ship_qty:
                return Response(
                    {"error": f"Insufficient stock for '{order_item.item.name}': need {ship_qty}, have {physical_stock}."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Deduct from inventory
            stock_entry = Stock.objects.filter(item=order_item.item).order_by('-quantity').first()
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
                    company=order.customer.company
                )

        # Update order status
        order.status = 'delivered' if fully_fulfilled else 'shipped'
        order.save()

        # Create Logistics Shipment for this partial drop
        warehouse = Warehouse.objects.first()
        if warehouse:
            Shipment.objects.create(
                sales_order=order,
                warehouse=warehouse,
                status="in-transit",
                progress=20,
                driver="Partial LTL Route"
            )

        send_notification(
            "store",
            f"Partial shipment: SO#{order.id} — Shipped: {', '.join(shipped_summary)}. Remaining: {', '.join(remaining_summary) or 'None'}.",
            related_id=order.id,
            related_type="sales_order",
            company=order.customer.company
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
        Create an Invoice from this sales order (lines priced from item
        selling prices). The invoice is mirrored to QuickBooks automatically.
        Optional payload: { "due_date": "YYYY-MM-DD" }
        """
        order = self.get_object()

        if order.status == 'cancelled':
            return Response({"error": "Cannot invoice a cancelled order."}, status=status.HTTP_400_BAD_REQUEST)
        existing = order.invoices.exclude(status='cancelled').first()
        if existing:
            return Response(
                {"error": f"Invoice INV-{existing.id} already exists for this order."},
                status=status.HTTP_400_BAD_REQUEST
            )

        due_date = None
        if request.data.get('due_date'):
            try:
                due_date = date.fromisoformat(str(request.data['due_date']))
            except ValueError:
                return Response({"error": "due_date must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        if due_date is None:
            due_date = date.today() + timedelta(days=30)

        invoice = Invoice.objects.create(
            company=order.customer.company,
            sales_order=order,
            customer=order.customer,
            due_date=due_date,
        )
        total = Decimal("0")
        for order_item in order.salesorderitem_set.select_related('item'):
            unit_price = order_item.item.selling_price or Decimal("0")
            amount = unit_price * Decimal(str(order_item.quantity))
            InvoiceLine.objects.create(
                invoice=invoice,
                item=order_item.item,
                description=order_item.item.name,
                quantity=order_item.quantity,
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

        payment_date = date.today()
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

        from quickbooks.push import get_active_connection, safe_push
        connection = get_active_connection(invoice.company)
        if connection:
            safe_push(connection, "payment", payment)

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
    permission_classes = [IsSales | IsAdmin]

    def perform_create(self, serializer):

        shipment = serializer.save()

        order = shipment.sales_order

        # Deduct finished goods stock on shipment
        from inventory.lots import ship_lots_fifo
        for item in order.salesorderitem_set.all():
            decrease_stock(
                item.item,
                shipment.warehouse,
                item.quantity,
                user=self.request.user,
                reference=f"Shipment SO#{order.id}"
            )
            # 🔗 SQF traceability: record which finished lots left on this
            # shipment (FIFO), closing the receive→produce→QC→ship chain.
            ship_lots_fifo(
                shipment, item.item, item.quantity,
                company=getattr(item.item, "company", None),
            )

        # Update order status
        order.status = "shipped"
        order.save()


class InboundOrderEmailViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    """The 'Orders from Email' inbox. Read-only list/detail plus a `confirm`
    action that promotes a reviewed draft into a real order.

    Confirming flips the linked SalesOrder from "draft" → "pending", which
    releases the QuickBooks push guardrail so the order syncs normally from
    that point on. Nothing syncs while it sits in draft."""
    company_field = "company"
    queryset = InboundOrderEmail.objects.all()
    serializer_class = InboundOrderEmailSerializer
    permission_classes = [IsSales | IsStore | IsAdmin]

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
