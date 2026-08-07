from datetime import date

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend

from .models import (
    Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt,
    Bill, BillLine, VendorEmail,
)
from .serializers import (
    VendorSerializer, VendorPriceListSerializer,
    PurchaseOrderSerializer, PurchaseOrderItemSerializer, GoodsReceiptSerializer,
    BillSerializer, VendorEmailSerializer,
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

        # Placing the order is the point at which the vendor needs to hear from
        # us. A single-order update is its own ordering action, so it gets its
        # own email; batches go through `bulk_order` instead.
        if new_status == "ordered":
            self._draft_emails([po])

    @staticmethod
    def _draft_emails(orders):
        """Draft vendor emails for a batch of just-placed orders. Never fatal —
        a composition failure must not cost the user the order itself."""
        from .emails import draft_for_orders

        try:
            return draft_for_orders(orders)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Could not draft vendor email(s) for PO(s) %s",
                [o.id for o in orders], exc_info=True,
            )
            return []

    @action(detail=False, methods=["post"])
    def bulk_order(self, request):
        """Place several purchase orders as ONE ordering action.

        This is what the "Order All" button calls. Because the whole selection
        arrives together, orders can be grouped by vendor into a single email
        each — which per-order PATCHes could never achieve, since each request
        is blind to the others.

        Body: {"ids": [1, 2, 3]}
        """
        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return Response(
                {"error": "Provide a non-empty 'ids' list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # get_queryset() is company-scoped, so this cannot reach another tenant.
        orders = list(self.get_queryset().filter(id__in=ids).select_related("vendor"))
        found_ids = {o.id for o in orders}
        missing = [i for i in ids if i not in found_ids]

        placed, rejected = [], []
        for po in orders:
            if po.status not in ("draft", "pending", "approved"):
                rejected.append({"id": po.id, "error": f"Cannot order a {po.status} order."})
                continue
            po.status = "ordered"
            po.save(update_fields=["status"])
            placed.append(po)
            log_activity(
                request.user, "Procurement", "Order Purchase Order",
                f"PO #{po.id} placed with vendor '{po.vendor.name}'",
            )

        drafts = self._draft_emails(placed)

        return Response({
            "ordered": [po.id for po in placed],
            "rejected": rejected,
            "missing": missing,
            "emails_created": [
                {
                    "id": d.id,
                    "vendor": d.vendor.name,
                    "purchase_orders": [f"PO-{p.id:04d}" for p in d.purchase_orders.all()],
                }
                for d in drafts
            ],
        })

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


class VendorEmailViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Mail Center: drafts generated from purchase orders, plus their history.

    Sending is not implemented. `send` returns 501 so the UI can surface a
    truthful "not configured yet" message rather than pretending to deliver.
    """
    company_field = "company"
    queryset = (
        VendorEmail.objects
        .select_related("vendor", "sent_by")
        .prefetch_related("purchase_orders", "attachments")
        .all()
    )
    serializer_class = VendorEmailSerializer
    permission_classes = [IsStore | IsAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["vendor", "status", "purchase_orders"]

    def perform_update(self, serializer):
        email = serializer.save()
        log_activity(
            self.request.user, "Procurement", "Edit Vendor Email",
            f"Edited draft email #{email.id} to '{email.vendor.name}'",
        )

    def perform_destroy(self, instance):
        """Delete a draft. Sent mail is a record of what a vendor received and
        is never deletable — losing it would break the communication log the
        order relies on."""
        if instance.status == "sent":
            raise ValidationError(
                {"error": "Sent emails cannot be deleted; they are part of the order's record."}
            )
        log_activity(
            self.request.user, "Procurement", "Delete Vendor Email",
            f"Deleted {instance.status} email #{instance.id} to '{instance.vendor.name}'",
        )
        instance.delete()

    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        """Delete several drafts at once.

        Body: {"ids": [1,2,3]}  — or {"all_drafts": true} to clear every draft.
        Sent emails are always excluded and reported back, never silently
        skipped, so the caller knows exactly what was kept.
        """
        qs = self.get_queryset()  # company-scoped

        if request.data.get("all_drafts"):
            targets = list(qs.filter(status="draft"))
            protected = []
        else:
            ids = request.data.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return Response(
                    {"error": "Provide a non-empty 'ids' list, or 'all_drafts': true."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            selected = list(qs.filter(id__in=ids))
            targets = [e for e in selected if e.status != "sent"]
            protected = [e.id for e in selected if e.status == "sent"]

        deleted_ids = [e.id for e in targets]
        count = len(deleted_ids)
        if count:
            self.get_queryset().filter(id__in=deleted_ids).delete()
            log_activity(
                request.user, "Procurement", "Delete Vendor Emails",
                f"Deleted {count} vendor email draft(s)",
            )

        return Response({
            "deleted": count,
            "deleted_ids": deleted_ids,
            "protected_sent": protected,
        })

    @action(detail=True, methods=["post"])
    def regenerate(self, request, pk=None):
        """Rebuild the body from the covered orders, discarding manual edits."""
        from .emails import compose_body, compose_subject

        email = self.get_object()
        if email.status != "draft":
            return Response(
                {"error": f"Only drafts can be regenerated. This email is {email.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        orders = list(email.purchase_orders.prefetch_related("items__item").order_by("id"))
        email.subject = compose_subject(email.vendor, orders, email.company)
        email.body_html = compose_body(email.vendor, orders, email.company)
        email.body_edited = False
        email.save()
        return Response(VendorEmailSerializer(email).data)

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Placeholder for the future SMTP transport.

        Returns 501 rather than silently marking the draft sent — reporting a
        delivery that never happened would corrupt the audit trail this model
        exists to keep.
        """
        email = self.get_object()
        if email.status != "draft":
            return Response(
                {"error": f"This email is already {email.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "error": "Email sending is not configured yet.",
                "detail": (
                    "SMTP delivery is planned for a future release. The draft has "
                    "been saved and will be ready to send once configured."
                ),
            },
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


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

        # Close the loop back to production. Material was procured because a
        # production order was short of it; now that it has landed, the people
        # waiting on it have to be told, or the batch sits blocked indefinitely.
        self._notify_waiting_production(po, receipt)

        log_activity(
            self.request.user, "Procurement", "Goods Receipt",
            f"Received goods for PO #{po.id} into '{receipt.warehouse.name}': {', '.join(items_received)}"
        )

    @staticmethod
    def _notify_waiting_production(po, receipt):
        """Release the inventory requests this PO was raised to fill and tell
        production once per production order.

        A batch can be short of several materials, all covered by one PO. The
        per-request signal would then fire once per material, so it is
        suppressed here and replaced with a single summary per production
        order — and, when nothing is outstanding, that summary says the order
        is fully supplied and ready to run.

        Never fatal: a failure here must not undo a receipt that moved stock.
        """
        from core.utils import send_notification
        from inventory.models import InventoryRequest

        try:
            requests = list(
                InventoryRequest.objects
                .filter(purchase_order=po, status__in=["pending", "procuring"])
                .select_related("item", "warehouse", "production_order")
            )
            if not requests:
                return

            for req in requests:
                # The summary below replaces the per-request message.
                req._suppress_supply_notification = True
                req.status = "supplied"
                req.save(update_fields=["status"])

            company = getattr(po.vendor, "company", None)

            # Group by production order so each waiting batch gets one message.
            by_order = {}
            for req in requests:
                by_order.setdefault(req.production_order_id, []).append(req)

            for prod_id, reqs in by_order.items():
                materials = ", ".join(
                    sorted({f"{r.quantity:g} {r.item.unit} {r.item.name}" for r in reqs})
                )
                warehouse = reqs[0].warehouse.name

                if prod_id is None:
                    # Not raised for a batch — a plain restock.
                    send_notification(
                        "production",
                        f"Material received at {warehouse}: {materials}.",
                        related_id=po.id, related_type="PurchaseOrder",
                        company=company, module="production",
                    )
                    continue

                # Anything still outstanding on this batch, beyond what just landed.
                still_waiting = (
                    InventoryRequest.objects
                    .filter(production_order_id=prod_id, status__in=["pending", "procuring"])
                    .select_related("item")
                )
                pending_names = sorted({r.item.name for r in still_waiting})

                if pending_names:
                    tail = (
                        f" Still awaiting: {', '.join(pending_names)}."
                        if len(pending_names) <= 4
                        else f" Still awaiting {len(pending_names)} other materials."
                    )
                    headline = f"Materials received for Production Order #{prod_id}"
                else:
                    tail = " All materials are now in stock — production can start."
                    headline = f"Production Order #{prod_id} is fully supplied"

                send_notification(
                    "production",
                    f"{headline} at {warehouse}: {materials}.{tail}",
                    related_id=prod_id, related_type="ProductionOrder",
                    company=company, module="production",
                )

            names = ", ".join(sorted({r.item.name for r in requests}))
            send_notification(
                "store",
                f"PO #{po.id} received — {names} released to production.",
                related_id=po.id, related_type="PurchaseOrder",
                company=company, module="inventory",
            )
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Could not notify production for PO #%s", po.id, exc_info=True
            )
