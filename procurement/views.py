from datetime import date
from decimal import Decimal
from django.db import transaction

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from .models import (
    Vendor, VendorPriceList, PurchaseOrder, PurchaseOrderItem, GoodsReceipt,
    Bill, BillLine, VendorEmail, ScheduledPurchaseOrder,
    Bill, BillLine, VendorEmail, PurchaseRequisition, PurchaseRequisitionItem,
)
from .serializers import (
    VendorSerializer, VendorPriceListSerializer,
    PurchaseOrderSerializer, PurchaseOrderItemSerializer, GoodsReceiptSerializer,
    BillSerializer, VendorEmailSerializer, ScheduledPurchaseOrderSerializer,
    BillSerializer, VendorEmailSerializer,
    PurchaseRequisitionSerializer, PurchaseRequisitionItemSerializer,
)
from inventory.models import Item, Warehouse
from inventory.serializers import ItemSerializer
from inventory.services import increase_stock
from accounts.permission import IsStore, IsAdmin, IsFinance, IsQuality, IsProduction
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
    queryset = PurchaseOrder.objects.prefetch_related("items__item", "bills__payments").select_related("vendor").all()
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

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def pay_bill(self, request, pk=None):
        """Pay this order's vendor bill from the Procurement screen (admins).

        Goes through the same accounts-payable flow as Accounting > Accounts
        Payable, atomically: the bill is marked partial/paid, a VendorPayment is
        recorded and Dr Accounts Payable / Cr Bank is posted to the ledger. If
        the ledger posting is refused (closed period, no chart of accounts) the
        whole payment is rolled back.

        Body: {"amount": optional (default: balance due), "method": "bank_transfer",
               "reference": "", "payment_date": "YYYY-MM-DD"}
        """
        from decimal import Decimal, InvalidOperation
        from django.core.exceptions import ValidationError as DjangoValidationError
        from accounting.payables import record_and_allocate_ap_payment

        po = self.get_object()
        bill = po.bills.exclude(status="cancelled").first()
        if bill is None:
            return Response({"error": "This order has no vendor bill yet. It is created when the goods are received."},
                            status=status.HTTP_400_BAD_REQUEST)
        if bill.status == "paid":
            return Response({"error": "This bill is already paid."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            amount = Decimal(str(request.data.get("amount") or bill.balance_due))
        except InvalidOperation:
            return Response({"error": "amount must be a number."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            record_and_allocate_ap_payment(
                vendor_id=po.vendor_id,
                amount=amount,
                user=request.user,
                company=request.user.company,
                payment_date=request.data.get("payment_date") or None,
                method=request.data.get("method") or "bank_transfer",
                reference=request.data.get("reference", ""),
                allocations=[{"bill_id": bill.id, "amount": str(amount)}],
            )
        except DjangoValidationError as e:
            return Response({"error": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({"error": "payment_date must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        log_activity(request.user, "Procurement", "Pay Vendor Bill",
                     f"Paid {amount} on {bill.bill_number or f'BILL-{bill.id}'} for PO #{po.id}")
        po = self.get_queryset().get(pk=po.pk)
        return Response(self.get_serializer(po).data)

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
            bill_date = parse_date("bill_date", timezone.localdate())
            due_date = parse_date("due_date")
        except ValueError:
            return Response({"error": "Dates must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        from .billing import create_bill_from_po, push_bill_to_quickbooks
        bill = create_bill_from_po(
            po, bill_date=bill_date, due_date=due_date, bill_number=request.data.get("bill_number", ""),
        )
        push_bill_to_quickbooks(bill)

        from accounting.auto_posting import queue_auto_post
        queue_auto_post("vendor_bill", bill.company, bill.id, request.user)

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

    @transaction.atomic
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
        from production.models import ManufacturingSettings
        # Track B (TB-04): with incoming QC on, received material is held as a
        # QA-pending lot and only becomes usable stock when quality accepts it.
        hold_for_qc = ManufacturingSettings.for_company(po.vendor.company).incoming_qc_required
        for poi in po.items.all():
            received_base_quantity = poi.quantity
            if poi.unit_of_measure_id and poi.item.base_unit_id:
                from inventory.uom import UomConversionError, convert
                try:
                    received_base_quantity = float(convert(
                        poi.quantity, poi.unit_of_measure, poi.item.base_unit, item=poi.item
                    ))
                except UomConversionError as exc:
                    raise ValidationError({
                        "items": f"Cannot receive {poi.item.name} from {poi.unit_of_measure.code} "
                                 f"into stock unit {poi.item.base_unit.code}: {exc}"
                    })
            if not hold_for_qc:
                increase_stock(
                    poi.item, receipt.warehouse, received_base_quantity,
                    user=self.request.user, reference=f"GRN PO#{po.id}",
                )
            # 🔗 SQF traceability: every received line becomes a raw lot tied to
            # this goods receipt (and thus the vendor delivery).
            lot = create_raw_lot(
                poi.item, receipt.warehouse, received_base_quantity, receipt,
                company=getattr(poi.item, "company", None),
            )
            if hold_for_qc:
                from quality.models import QualityCheck
                lot.qa_status, lot.remaining_quantity = "pending", 0
                lot.save(update_fields=["qa_status", "remaining_quantity"])
                QualityCheck.objects.create(
                    inspection_type="incoming", goods_receipt=receipt, vendor=po.vendor,
                    item=poi.item, warehouse=receipt.warehouse, lot=lot,
                    received_quantity=received_base_quantity, status="pending",
                    test_type="Incoming Inspection",
                )
            items_received.append(
                f"{poi.quantity} {poi.unit_of_measure.code if poi.unit_of_measure_id else poi.item.unit} x {poi.item.name}"
            )
        if hold_for_qc:
            from core.utils import send_notification
            send_notification(
                "quality",
                f"Goods received for PO #{po.id} ({', '.join(items_received)}) await incoming inspection.",
                related_id=receipt.id, related_type="GoodsReceipt",
                company=po.vendor.company, module="quality",
            )
        po.status = "received"
        po.save()
        from finance.services import record_procurement_cost
        record_procurement_cost(po, user=self.request.user)
        # Ledger posting and (per Accounting setup) the vendor bill.
        from .billing import on_goods_received
        on_goods_received(po, receipt, self.request.user)

        log_activity(
            self.request.user, "Procurement", "Goods Receipt",
            f"Received goods for PO #{po.id} at '{receipt.warehouse.name}': {', '.join(items_received)}"
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


class ScheduledPurchaseOrderViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Purchase orders to place automatically on a date (AI Procurement).

    Deleting cancels the schedule (kept for history). Editing a failed schedule
    re-arms it. `place_now` places it immediately instead of waiting."""
    company_field = "company"
    queryset = ScheduledPurchaseOrder.objects.select_related("item", "vendor", "purchase_order", "created_by")
    serializer_class = ScheduledPurchaseOrderSerializer
    permission_classes = [IsStore | IsAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "item", "vendor"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_create(self, serializer):
        schedule = serializer.save(company=self.request.user.company, created_by=self.request.user)
        log_activity(
            self.request.user, "Procurement", "Schedule Purchase Order",
            f"Scheduled {schedule.quantity:g} x {schedule.item.name} for {schedule.scheduled_date} ({schedule.get_repeat_display()})",
        )

    def perform_update(self, serializer):
        extra = {"status": "scheduled"}
        if "scheduled_date" in serializer.validated_data:
            extra["anchor_day"] = serializer.validated_data["scheduled_date"].day
        schedule = serializer.save(**extra)
        log_activity(self.request.user, "Procurement", "Edit Scheduled Order",
                     f"Scheduled order #{schedule.id} now {schedule.quantity:g} x {schedule.item.name} on {schedule.scheduled_date}")

    def destroy(self, request, *args, **kwargs):
        schedule = self.get_object()
        if schedule.status not in ("scheduled", "failed"):
            return Response({"error": f"A {schedule.status} schedule cannot be cancelled."},
                            status=status.HTTP_400_BAD_REQUEST)
        schedule.status = "cancelled"
        schedule.save(update_fields=["status", "updated_at"])
        log_activity(request.user, "Procurement", "Cancel Scheduled Order", f"Cancelled scheduled order #{schedule.id}")
        return Response(self.get_serializer(schedule).data)

    @action(detail=True, methods=["post"])
    def place_now(self, request, pk=None):
        from .scheduled import claim, place_scheduled_order

        schedule = self.get_object()
        if not claim(schedule.id, from_statuses=("scheduled", "failed")):
            return Response({"error": f"This schedule is {schedule.status} and cannot be placed now."},
                            status=status.HTTP_400_BAD_REQUEST)
        schedule = place_scheduled_order(ScheduledPurchaseOrder.objects.get(pk=schedule.id))
        return Response(self.get_serializer(schedule).data)


class PurchaseRequisitionViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = (
        PurchaseRequisition.objects
        .select_related("company", "warehouse", "production_plan", "sales_order", "created_by")
        .prefetch_related("items__item", "purchase_orders")
        .all()
    )
    serializer_class = PurchaseRequisitionSerializer
    permission_classes = [IsStore | IsAdmin | IsProduction]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "production_plan", "sales_order"]

    @action(detail=False, methods=["post"])
    def create_from_mrp(self, request):
        """
        Creates or retrieves a persistent PurchaseRequisition from an MRP shortage result.
        Idempotent: If an active requisition already exists for this production plan,
        it returns the existing requisition without creating duplicates.
        """
        data = request.data
        plan_id = data.get("production_plan_id") or data.get("production_plan")
        if not plan_id:
            return Response({"error": "production_plan_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        from production.models import ProductionPlan
        plan = ProductionPlan.objects.filter(id=plan_id).first()
        if not plan:
            return Response({"error": f"Production plan #{plan_id} not found"}, status=status.HTTP_404_NOT_FOUND)

        user_company = getattr(request.user, "company", None)
        if user_company and plan.company and plan.company != user_company and not getattr(request.user, "is_superuser", False):
            return Response({"error": "You do not have permission to access a production plan from another company."}, status=status.HTTP_403_FORBIDDEN)

        company = plan.company or request.user.company

        # 🔒 Idempotency: verify no existing non-cancelled requirement for this plan
        existing_req = (
            PurchaseRequisition.objects
            .filter(production_plan=plan, status__in=["draft", "submitted", "approved", "converted"])
            .first()
        )
        if existing_req:
            return Response(
                {
                    "message": f"Purchase Requisition {existing_req.requisition_number} already exists for plan {plan.plan_number}.",
                    "requisition": PurchaseRequisitionSerializer(existing_req).data,
                    "created": False,
                },
                status=status.HTTP_200_OK,
            )

        warehouse_id = data.get("warehouse_id") or data.get("warehouse")
        warehouse = None
        if warehouse_id:
            warehouse = Warehouse.objects.filter(id=warehouse_id).first()
        if not warehouse and company:
            warehouse = Warehouse.objects.filter(company=company, is_quarantine=False).first()

        raw_items = data.get("items") or []
        if not raw_items:
            # GAP 3: Automatically compute shortages from ProductionPlan using the MRP calculation engine
            try:
                from production.mrp import calculate_mrp_for_plan
                mrp_res = calculate_mrp_for_plan(plan, warehouse=warehouse)
            except Exception as e:
                return Response({"error": f"Failed to calculate MRP for plan: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

            # Extract shortage candidates from raw_materials list
            raw_candidates = mrp_res.get("raw_materials") or []
            if not raw_candidates:
                raw_candidates = [
                    c for c in mrp_res.get("consolidated", [])
                    if c.get("category") != "intermediate"
                ]

            shortage_candidates = [
                r for r in raw_candidates
                if float(r.get("shortage_quantity", 0)) > 0
            ]

            if not shortage_candidates:
                return Response(
                    {
                        "message": f"No material shortages detected for production plan {plan.plan_number}. No purchase requisition required.",
                        "created": False,
                        "has_shortage": False,
                    },
                    status=status.HTTP_200_OK,
                )

            raw_items = [
                {
                    "item_id": item_res["item_id"],
                    "required_quantity": item_res["required_quantity"],
                    "available_quantity": item_res["available_quantity"],
                    "shortage_quantity": item_res["shortage_quantity"],
                    "uom": item_res.get("unit") or "unit",
                }
                for item_res in shortage_candidates
            ]
        else:
            shortage_items = [it for it in raw_items if float(it.get("shortage_quantity", 0)) > 0]
            if not shortage_items:
                return Response(
                    {
                        "message": f"No material shortages detected for production plan {plan.plan_number}. No purchase requisition required.",
                        "created": False,
                        "has_shortage": False,
                    },
                    status=status.HTTP_200_OK,
                )
            raw_items = shortage_items

        with transaction.atomic():
            req = PurchaseRequisition.objects.create(
                company=company,
                warehouse=warehouse,
                production_plan=plan,
                sales_order=plan.sales_order,
                status="approved",
                notes=data.get("notes", f"Generated from MRP shortage on {plan.plan_number}"),
                created_by=request.user,
            )

            created_items = []
            for item_data in raw_items:
                item_id = item_data.get("item_id") or item_data.get("item")
                item = Item.objects.filter(id=item_id).first()
                if not item:
                    continue
                req_qty = float(item_data.get("required_quantity", 0))
                avail_qty = float(item_data.get("available_quantity", 0))
                shortage_qty = float(item_data.get("shortage_quantity", req_qty))
                uom = item_data.get(
                    "uom",
                    getattr(item.base_uom, "code", "unit") if hasattr(item, "base_uom") and item.base_uom else (item.unit or "unit")
                )

                v_price = VendorPriceList.objects.filter(item=item, is_active=True).first()
                est_price = v_price.unit_price if v_price else (item.purchase_cost or Decimal("0.00"))

                PurchaseRequisitionItem.objects.create(
                    requisition=req,
                    item=item,
                    required_quantity=req_qty,
                    available_quantity=avail_qty,
                    shortage_quantity=shortage_qty,
                    uom=uom,
                    estimated_unit_price=est_price,
                )
                created_items.append(f"{shortage_qty} {uom} of {item.name}")

            log_activity(
                request.user, "Procurement", "Create Purchase Requirement",
                f"Created {req.requisition_number} from MRP for {plan.plan_number}: {', '.join(created_items)}"
            )

        return Response(
            {
                "message": f"Successfully created Purchase Requisition {req.requisition_number}",
                "requisition": PurchaseRequisitionSerializer(req).data,
                "created": True,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        req = self.get_object()
        if req.status in ["approved", "converted"]:
            return Response({"error": f"Requisition is already {req.status}."}, status=status.HTTP_400_BAD_REQUEST)
        if req.status == "cancelled":
            return Response({"error": "Cannot approve a cancelled requisition."}, status=status.HTTP_400_BAD_REQUEST)
        req.status = "approved"
        req.save(update_fields=["status", "updated_at"])
        log_activity(request.user, "Procurement", "Approve Requirement", f"Approved {req.requisition_number}")
        return Response(PurchaseRequisitionSerializer(req).data)

    @action(detail=True, methods=["post"])
    def convert_to_po(self, request, pk=None):
        """
        Converts an approved Purchase Requisition into a Purchase Order atomically.
        Retains traceability to PurchaseRequisition and ProductionPlan.
        """
        req = self.get_object()
        if req.status == "converted":
            return Response({"error": "This requirement has already been converted to a Purchase Order."}, status=status.HTTP_400_BAD_REQUEST)
        if req.status == "cancelled":
            return Response({"error": "Cannot convert a cancelled requirement."}, status=status.HTTP_400_BAD_REQUEST)
        if req.status not in ["approved", "submitted", "draft"]:
            return Response({"error": f"Requisition must be approved before conversion (current status: {req.status})."}, status=status.HTTP_400_BAD_REQUEST)

        vendor_id = request.data.get("vendor_id") or request.data.get("vendor")

        with transaction.atomic():
            vendor = None
            if vendor_id:
                vendor = Vendor.objects.filter(id=vendor_id).first()

            items_to_procure = list(req.items.select_related("item").all())
            if not items_to_procure:
                return Response({"error": "No items found in this requisition."}, status=status.HTTP_400_BAD_REQUEST)

            if not vendor:
                first_item = items_to_procure[0].item
                vp = VendorPriceList.objects.filter(item=first_item, is_active=True).first()
                if vp:
                    vendor = vp.vendor
                else:
                    vendor = Vendor.objects.filter(company=req.company).first()
                if not vendor:
                    vendor = Vendor.objects.first()

            if not vendor:
                return Response({"error": "No vendor available to raise Purchase Order. Please configure a vendor first."}, status=status.HTTP_400_BAD_REQUEST)

            po = PurchaseOrder.objects.create(
                vendor=vendor,
                requisition=req,
                status="pending",
                priority="high",
                notes=f"Converted from {req.requisition_number} (Plan: {req.production_plan.plan_number if req.production_plan else 'N/A'})",
            )

            for req_item in items_to_procure:
                vp = VendorPriceList.objects.filter(vendor=vendor, item=req_item.item, is_active=True).first()
                unit_price = vp.unit_price if vp else (req_item.estimated_unit_price or req_item.item.purchase_cost or Decimal("0.00"))
                item = req_item.item
                purchase_unit = item.purchase_unit or item.base_unit
                shortage_purchase_quantity = float(req_item.shortage_quantity)
                if item.base_unit and purchase_unit and item.base_unit_id != purchase_unit.id:
                    if req_item.uom == item.base_unit.code:
                        from inventory.uom import UomConversionError, convert
                        try:
                            shortage_purchase_quantity = float(convert(
                                req_item.shortage_quantity, item.base_unit, purchase_unit, item=item
                            ))
                        except UomConversionError as exc:
                            raise ValidationError({"unit_of_measure": str(exc)})
                    elif req_item.uom != purchase_unit.code:
                        raise ValidationError({
                            "uom": f"Requisition unit '{req_item.uom}' cannot be reconciled with "
                                   f"{item.base_unit.code} or {purchase_unit.code} for {item.name}."
                        })
                item_moq = float(item.minimum_order_quantity or 0)
                vendor_moq = float(vp.min_order_qty if vp else 0)
                po_qty = max(shortage_purchase_quantity, item_moq, vendor_moq)

                PurchaseOrderItem.objects.create(
                    purchase_order=po,
                    item=item,
                    quantity=po_qty,
                    unit_of_measure=purchase_unit,
                    unit_price=unit_price,
                )

            po.recalculate_total()
            req.status = "converted"
            req.save(update_fields=["status", "updated_at"])

            log_activity(
                request.user, "Procurement", "Convert Requisition to PO",
                f"Converted {req.requisition_number} into PO #{po.id} with vendor '{vendor.name}'"
            )

        return Response(
            {
                "message": f"Successfully converted {req.requisition_number} to PO #{po.id}",
                "purchase_order_id": po.id,
                "purchase_order": PurchaseOrderSerializer(po).data,
                "requisition": PurchaseRequisitionSerializer(req).data,
            },
            status=status.HTTP_201_CREATED,
        )
