"""Quality Check Engine (Track B, TB-04 / TB-05 / TB-18).

A QA decision splits the inspected quantity into accepted / rejected /
quarantined / rework. Only the accepted quantity becomes usable stock:

  * production output: accepted units are added to finished-goods stock and the
    output lot becomes QA Approved; the other portions are split into child
    lots (rejected / quarantine / rework) that are never sellable.
  * incoming raw material: accepted units are added to raw-material stock; the
    rest stays out of stock in rejected / quarantine lots.

Produced FG is therefore never the same as sellable FG until QA says so.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.models import Batch
from inventory.services import increase_stock

from .models import QualityCheck

EPS = 1e-6


def _num(value):
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("QA quantities must be numbers.")
    if number < 0:
        raise ValidationError("QA quantities cannot be negative.")
    return number


def _split_lot(lot, quantity, qa_status):
    """Create a child lot holding `quantity` of `lot` in `qa_status`."""
    suffix = {"rejected": "REJ", "quarantine": "QRN", "rework": "RWK"}[qa_status]
    return Batch.objects.create(
        item=lot.item,
        batch_number=f"{lot.batch_number}-{suffix}",
        quantity=quantity,
        remaining_quantity=quantity,
        expiry_date=lot.expiry_date,
        company=lot.company,
        warehouse=lot.warehouse,
        source=lot.source,
        goods_receipt=lot.goods_receipt,
        production_order=lot.production_order,
        qa_status=qa_status,
        parent=lot,
    )


def _resolve_quantities(qc, data):
    inspected = qc.inspected_quantity
    accepted = data.get("accepted_quantity")
    rejected = _num(data.get("rejected_quantity"))
    quarantined = _num(data.get("quarantined_quantity"))
    rework = _num(data.get("rework_quantity"))
    if qc.inspection_type == "incoming" and rework:
        raise ValidationError("Incoming material cannot be sent to rework; reject or quarantine it.")
    if accepted in (None, ""):
        accepted = max(inspected - rejected - quarantined - rework, 0)
    accepted = _num(accepted)
    total = accepted + rejected + quarantined + rework
    if abs(total - inspected) > EPS:
        raise ValidationError(
            f"Accepted + rejected + quarantined + rework must equal the inspected quantity "
            f"({inspected:g}); got {total:g}."
        )
    sample = data.get("sample_quantity")
    if sample not in (None, "") and _num(sample) > inspected + EPS:
        raise ValidationError("Sample quantity cannot exceed the inspected quantity.")
    return accepted, rejected, quarantined, rework


def _decision(accepted, rejected, quarantined, rework):
    if accepted > EPS and rejected + quarantined + rework <= EPS:
        return "pass"
    if accepted > EPS:
        return "partial"
    if rework > EPS and rejected + quarantined <= EPS:
        return "rework"
    if quarantined > EPS and rejected + rework <= EPS:
        return "quarantine"
    return "fail"


@transaction.atomic
def decide(qc, data, user=None):
    """Record a QA decision. `data` holds accepted/rejected/quarantined/rework
    quantities plus optional sample_quantity, remarks, result and parameters."""
    qc = QualityCheck.objects.select_for_update().get(pk=qc.pk)
    if qc.status != "pending":
        raise ValidationError("Quality decision already made")
    if qc.inspection_type == "production":
        order = qc.production_order
        if order is None:
            raise ValidationError("This check is not linked to a production order.")
        if qc.output_id is None and order.status != "completed":
            raise ValidationError("Production not completed yet")

    accepted, rejected, quarantined, rework = _resolve_quantities(qc, data)
    decision = _decision(accepted, rejected, quarantined, rework)

    qc.accepted_quantity = accepted
    qc.rejected_quantity = rejected
    qc.quarantined_quantity = quarantined
    qc.rework_quantity = rework
    if qc.received_quantity is None:
        qc.received_quantity = qc.inspected_quantity
    if data.get("sample_quantity") not in (None, ""):
        qc.sample_quantity = _num(data.get("sample_quantity"))
    for field in ("remarks", "result", "test_type", "parameter", "target"):
        if data.get(field) not in (None, ""):
            setattr(qc, field, data[field])
    if isinstance(data.get("parameters"), list):
        qc.parameters = data["parameters"]
    qc.result_decision = decision
    qc.status = "approved" if accepted > EPS else "rejected"
    qc.inspector = user if getattr(user, "is_authenticated", False) else None
    qc.decided_at = timezone.now()
    if accepted > EPS and not qc.certificate_number:
        qc.certificate_number = f"QAC-{qc.id:05d}"

    if qc.inspection_type == "incoming":
        _apply_incoming(qc, accepted, rejected, quarantined, user)
    elif _apply_production(qc, accepted, rejected, quarantined, rework, user) is False:
        # Supplementary test: keep the verdict, but its quantities must not be
        # counted again in QA totals, costing or the GL.
        qc.accepted_quantity = qc.rejected_quantity = None
        qc.quarantined_quantity = qc.rework_quantity = None
    qc.save()

    if qc.inspection_type == "production":
        refresh_order_qa_status(qc.production_order)
        maybe_post_manufacturing_accounting(qc.production_order, user)
    return qc


def _apply_production(qc, accepted, rejected, quarantined, rework, user):
    from inventory.lots import create_finished_lot

    order = qc.production_order
    product = order.recipe.product
    if qc.output_id is None and (
        order.outputs.exists()
        or order.qualitycheck_set.exclude(pk=qc.pk).filter(status__in=["approved", "rejected"]).exists()
    ):
        # A supplementary test on an order whose output is already covered by
        # another check: it records a result but must not move stock again.
        return False
    lot = qc.lot
    if lot is None:
        # Legacy check (pre Track B): no lot was minted at completion.
        lot = create_finished_lot(product, order.warehouse, qc.inspected_quantity, order,
                                  company=getattr(product, "company", None))
        qc.lot = lot
    if accepted > EPS:
        increase_stock(product, order.warehouse, accepted, user=user,
                       reference=f"QA approved Production #{order.id}")
    lot.qa_status = "approved" if accepted > EPS else (
        "rework" if rework > EPS and rejected + quarantined <= EPS else
        "quarantine" if quarantined > EPS and rejected + rework <= EPS else "rejected"
    )
    lot.remaining_quantity = accepted
    lot.save(update_fields=["qa_status", "remaining_quantity"])
    # Keep the main lot for the accepted part; split off the other portions.
    portions = [("rejected", rejected), ("quarantine", quarantined), ("rework", rework)]
    if accepted <= EPS:
        # The whole lot already carries the dominant disposition; split the rest.
        portions = [(s, q) for s, q in portions if s != lot.qa_status]
        lot.remaining_quantity = {"rejected": rejected, "quarantine": quarantined, "rework": rework}[lot.qa_status]
        lot.save(update_fields=["remaining_quantity"])
    for qa_status, quantity in portions:
        if quantity > EPS:
            _split_lot(lot, quantity, qa_status)

    if order.is_rework and rejected > EPS:
        # Rework units were already valued into finished goods on the original
        # order's entry; rejecting them again is a write-off.
        from production.execution import record_scrap
        record_scrap(order, product, rejected, user=user, reason="Rejected at rework QA", source="qa")

    from core.utils import send_notification
    company = getattr(product, "company", None)
    if rework > EPS:
        send_notification(
            "production",
            f"QA sent {rework:g} {product.unit} of {product.name} from {order} back for rework.",
            related_id=order.id, related_type="ProductionOrder", company=company, module="production",
        )


def _queue_incoming_rejection(qc, quantity, source_module, source_id, user):
    """Rejected incoming material leaves raw-material inventory (TB-14)."""
    if quantity <= EPS or not qc.goods_receipt_id or not qc.vendor_id:
        return
    from accounting.auto_posting import queue_auto_post
    queue_auto_post("incoming_rejection", qc.vendor.company, source_id, user, {
        "source_module": source_module, "receipt_id": qc.goods_receipt_id,
        "item_id": qc.item_id, "quantity": quantity,
    })


def _apply_incoming(qc, accepted, rejected, quarantined, user):
    _queue_incoming_rejection(qc, rejected, "quality.incoming_rejection", qc.id, user)
    lot = qc.lot
    if accepted > EPS:
        # "GRN PO#n" keeps the movement tied to the goods-receipt posting, so
        # the inventory subledger never books it a second time.
        po_id = qc.goods_receipt.purchase_order_id if qc.goods_receipt_id else 0
        increase_stock(qc.item, qc.warehouse, accepted, user=user,
                       reference=f"GRN PO#{po_id} QA release")
    if lot is not None:
        lot.qa_status = "approved" if accepted > EPS else ("quarantine" if quarantined > EPS else "rejected")
        lot.remaining_quantity = accepted if accepted > EPS else (quarantined or rejected)
        lot.save(update_fields=["qa_status", "remaining_quantity"])
        if accepted > EPS:
            for qa_status, quantity in (("rejected", rejected), ("quarantine", quarantined)):
                if quantity > EPS:
                    _split_lot(lot, quantity, qa_status)
        elif rejected > EPS and quarantined > EPS:
            _split_lot(lot, rejected, "rejected")


@transaction.atomic
def release_quarantine(lot, accepted, rejected, user=None):
    """Re-inspect a quarantined lot: accepted units become usable stock."""
    if lot.qa_status != "quarantine":
        raise ValidationError("Only quarantined lots can be released.")
    accepted, rejected = _num(accepted), _num(rejected)
    if abs(accepted + rejected - (lot.remaining_quantity or 0)) > EPS:
        raise ValidationError(f"Accepted + rejected must equal the quarantined quantity ({lot.remaining_quantity:g}).")
    if accepted > EPS:
        increase_stock(lot.item, lot.warehouse, accepted, user=user,
                       reference=f"QA released quarantine lot {lot.batch_number}")
        lot.qa_status = "approved"
        lot.remaining_quantity = accepted
        lot.save(update_fields=["qa_status", "remaining_quantity"])
        if rejected > EPS:
            _split_lot(lot, rejected, "rejected")
    else:
        lot.qa_status = "rejected"
        lot.save(update_fields=["qa_status"])
    if lot.goods_receipt_id and rejected > EPS:
        qc = QualityCheck.objects.filter(goods_receipt_id=lot.goods_receipt_id, item=lot.item).first()
        if qc:
            _queue_incoming_rejection(qc, rejected, "quality.quarantine_rejection", lot.id, user)
    if lot.production_order_id:
        refresh_order_qa_status(lot.production_order)
    return lot


def refresh_order_qa_status(order):
    checks = list(order.qualitycheck_set.all())
    if not checks:
        status = "not_started"
    else:
        pending = any(c.status == "pending" for c in checks)
        accepted = sum(c.accepted_quantity or 0 for c in checks if c.status != "pending")
        legacy_approved = any(c.status == "approved" and c.accepted_quantity is None for c in checks)
        decided_any = any(c.status != "pending" for c in checks)
        if pending and not decided_any:
            status = "pending"
        elif pending:
            status = "partial" if accepted > EPS or legacy_approved else "pending"
        elif legacy_approved or accepted >= order.produced_quantity - EPS and accepted > EPS:
            status = "passed"
        elif accepted > EPS:
            status = "partial"
        else:
            status = "failed"
    if order.qa_status != status:
        order.qa_status = status
        type(order).objects.filter(pk=order.pk).update(qa_status=status)
    return status


def maybe_post_manufacturing_accounting(order, user=None):
    """Post RM → WIP → FG once the order is finished and fully QA-decided.

    FG value covers accepted, quarantined and rework units (still company
    inventory); QA-rejected and shop-floor scrap units go to scrap.
    """
    if order.is_rework:
        return
    if order.status not in ("completed", "closed"):
        return
    if order.qualitycheck_set.filter(status="pending").exists():
        return
    from django.db.models import Sum
    from accounting.auto_posting import queue_auto_post

    agg = order.qualitycheck_set.aggregate(
        accepted=Sum("accepted_quantity"), rejected=Sum("rejected_quantity"),
        quarantined=Sum("quarantined_quantity"), rework=Sum("rework_quantity"),
    )
    legacy = order.qualitycheck_set.filter(status="approved", accepted_quantity__isnull=True).exists()
    good = (agg["accepted"] or 0) + (agg["quarantined"] or 0) + (agg["rework"] or 0)
    if legacy and good <= EPS:
        good = order.produced_quantity or order.quantity
    scrap = (agg["rejected"] or 0) + sum(op.scrap_quantity for op in order.operations.all())
    scrap = min(scrap, max(order.quantity - good, 0))
    payload = {"completed_qty": round(good, 6), "scrap_qty": round(scrap, 6)}
    queue_auto_post("production_completed", order.recipe.product.company, order.id, user, payload)
