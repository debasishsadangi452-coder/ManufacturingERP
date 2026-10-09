"""Work-in-progress tracker (Track B, TB-02).

Definitions (TB-20):
  Completed quantity       = output reported by the production order
  Production completion %  = completed / planned x 100
  Stage progress %         = mean over operations of min(processed / planned, 1)
  Current operation        = first operation that is not completed
  Bottleneck               = open operation with the largest queue (units that
                             finished the previous operation but not this one);
                             a paused / failed / rework operation always is one
  Expected completion      = planned end, or now + remaining operation minutes
"""
from datetime import timedelta

from django.utils import timezone

from .execution import build_operations, material_status, order_summary
from .models import ProductionOrder

WIP_STATUSES = ProductionOrder.ACTIVE_STATUSES + ("completed",)


def _processed(op):
    return op.completed_quantity + op.rejected_quantity + op.scrap_quantity + op.rework_quantity


def wip_row(order):
    ops = list(order.operations.select_related("machine", "manpower").order_by("sequence")) or build_operations(order)
    current = next((o for o in ops if o.status != "completed"), None)
    stage_progress = (
        sum(min(_processed(o) / o.planned_quantity, 1) for o in ops if o.planned_quantity) / len(ops) * 100
        if ops else 0
    )
    bottleneck = None
    worst_queue = 0
    previous_done = None
    for op in ops:
        if op.status in ("paused", "failed", "rework_required"):
            bottleneck = {"operation_id": op.id, "operation": f"Op {op.sequence} {op.name}",
                          "reason": op.get_status_display(), "queue": None}
            break
        if op.status != "completed" and previous_done is not None:
            queue = previous_done - _processed(op)
            if queue > worst_queue:
                worst_queue = queue
                bottleneck = {"operation_id": op.id, "operation": f"Op {op.sequence} {op.name}",
                              "reason": f"{queue:g} unit(s) waiting", "queue": queue}
        previous_done = op.completed_quantity
    remaining_minutes = sum(o.remaining_minutes for o in ops)
    expected = order.planned_end or (
        timezone.now() + timedelta(minutes=remaining_minutes) if remaining_minutes else None
    )
    delayed = bool(order.planned_end and order.status != "completed" and order.planned_end < timezone.now())
    summary = order_summary(order)
    so = order.sales_order
    resource = None
    if current:
        resource = " / ".join(r.name for r in (current.machine, current.manpower) if r) or None
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "status_display": order.get_status_display(),
        "qa_status": order.qa_status,
        "is_rework": order.is_rework,
        "customer_order_id": so.id if so else None,
        "customer_order": f"SO-{so.id}" if so else None,
        "customer": so.customer.name if so else None,
        "production_plan_ref": order.production_plan_ref,
        "product_id": order.recipe.product_id,
        "product": order.recipe.product.name,
        # Sub-products (semi-finished) are shown separately on the shop floor
        # and skip QA; finished goods go through the QA gate.
        "product_category": order.recipe.product.category,
        "is_sub_product": order.recipe.product.is_semi_finished,
        "unit": order.recipe.product.unit,
        "planned_quantity": order.quantity,
        "completed_quantity": order.produced_quantity,
        "remaining_quantity": order.remaining_quantity,
        "production_completion_percent": round(order.produced_quantity / order.quantity * 100, 1) if order.quantity else 0,
        "stage_progress_percent": round(stage_progress, 1),
        "current_operation": (
            {"id": current.id, "sequence": current.sequence, "name": current.name,
             "status": current.status, "status_display": current.get_status_display(),
             "completed_quantity": current.completed_quantity, "planned_quantity": current.planned_quantity}
            if current else None
        ),
        "current_location": (current.location if current and current.location else
                             (order.line.location if order.line_id else None)),
        "line": order.line.name if order.line_id else None,
        "current_resource": resource,
        "operations_total": len(ops),
        "operations_completed": sum(1 for o in ops if o.status == "completed"),
        "rejected_quantity": summary["op_rejected"] + summary["qa_rejected"],
        "rework_quantity": summary["op_rework"] + summary["qa_rework"],
        "scrap_quantity": summary["op_scrap"],
        "qa_pending_quantity": summary["qa_pending"],
        "qa_accepted_quantity": summary["qa_accepted"],
        "material_status": material_status(order),
        "bottleneck": bottleneck,
        "start_time": order.start_time,
        "planned_start": order.planned_start,
        "planned_end": order.planned_end,
        "expected_completion": expected,
        "actual_completion": order.end_time,
        "delayed": delayed,
    }


def wip_board(company, include_completed=False):
    statuses = WIP_STATUSES if include_completed else ProductionOrder.ACTIVE_STATUSES
    orders = (
        ProductionOrder.objects
        .filter(recipe__product__company=company, status__in=statuses)
        .select_related("recipe__product", "line", "sales_order__customer")
        .order_by("planned_end", "created_at")
    )
    return [wip_row(o) for o in orders]
