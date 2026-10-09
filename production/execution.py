"""Manufacturing execution (Track B: TB-01 production orders, TB-03 shop-floor
operations, TB-18 scrap and rework).

Every quantity change on the shop floor goes through this module so the
production order, its operations, its material ledger, its lots and its QA
checks can never disagree.

Material rules (the double-deduction fix):
  * `reserve_materials` issues whatever is on hand when a customer order is
    released to production and records it on ProductionMaterialRequirement.
  * `issue_materials` brings consumption up to the share of the requirement
    that the produced quantity needs:
        target     = required x produced / planned
        additional = target - already consumed
    so material that was reserved or issued earlier is never deducted again.

Finished-goods rules (QA gate):
  * Reporting output creates a QA-pending lot and a QA check. Nothing is added
    to sellable stock until QA accepts it (quality.services).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.models import Stock
from inventory.services import decrease_stock, increase_stock

from .models import (
    ManufacturingSettings,
    ProductionMaterialRequirement,
    ProductionOperation,
    ProductionOrder,
    ProductionOutput,
    ScrapRecord,
)

EPS = 1e-6


def _company(order):
    return getattr(order.recipe.product, "company", None)


def _fmt(value):
    return f"{value:,.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Conversion: production requirement -> production order
# ---------------------------------------------------------------------------

def conversion_checks(recipe, quantity, *, plan_approved=True):
    """Validate a production requirement before it becomes a production order.

    Returns a list of {"key", "ok", "blocking", "message"}. Blocking checks
    stop the conversion; the others are warnings shown to the planner.
    """
    from .planning import material_check

    checks = []

    def add(key, ok, message, blocking=True):
        checks.append({"key": key, "ok": bool(ok), "blocking": blocking, "message": message})

    add("plan_approved", plan_approved,
        "Production plan is approved." if plan_approved else "Production plan is not approved.")
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        qty = 0
    add("quantity", qty > 0, "Quantity is valid." if qty > 0 else "Quantity must be greater than zero.")

    ingredients = [ingredient for ingredient, _ in recipe.material_requirements(qty)] if recipe and qty > 0 else []
    add("bom", bool(ingredients),
        "Bill of materials found." if ingredients else "Product has no bill of materials / recipe ingredients.")
    bad_lines = [ing.item.name for ing in ingredients if not ing.quantity or ing.quantity <= 0]
    add("material_info", not bad_lines,
        "Every material has a quantity." if not bad_lines
        else f"Materials without a quantity: {', '.join(bad_lines)}.")

    steps = list(recipe.routing_steps.all()) if recipe else []
    add("operations", bool(steps),
        f"{len(steps)} routing operation(s) defined." if steps
        else "No routing defined; a single default 'Production' operation will be used.",
        blocking=False)
    unresourced = [s.name for s in steps if not s.machine_id and not s.manpower_id]
    has_line = bool(recipe and recipe.default_line_id)
    resources_ok = (steps and not unresourced) or (not steps and has_line)
    add("resources", resources_ok,
        "Resources identified." if resources_ok
        else ("Operations without a machine or manpower: " + ", ".join(unresourced)) if unresourced
        else "No production line or resources assigned.",
        blocking=False)

    if recipe and qty > 0 and ingredients:
        mc = material_check(recipe, qty)
        add("materials", mc["can_produce"],
            "All materials available." if mc["can_produce"] else "Material shortage: " + " ".join(mc["warnings"]),
            blocking=False)
    return checks


def build_operations(order):
    """Copy the recipe routing onto the order (idempotent)."""
    if order.operations.exists():
        return list(order.operations.all())
    location = order.line.location if order.line_id else ""
    if order.is_rework:
        return [ProductionOperation.objects.create(
            production_order=order, sequence=1, name="Rework",
            description=f"Rework of QA-failed output from {order.rework_of}",
            location=location, planned_quantity=order.quantity, status="ready",
        )]
    steps = list(order.recipe.routing_steps.all())
    if not steps:
        return [ProductionOperation.objects.create(
            production_order=order, sequence=1, name="Production",
            location=location, planned_quantity=order.quantity, is_default=True,
            status="ready",
        )]
    ops = []
    for index, step in enumerate(steps):
        ops.append(ProductionOperation.objects.create(
            production_order=order,
            routing_step=step,
            sequence=step.sequence,
            name=step.name,
            description=step.description,
            location=step.location or location,
            planned_quantity=order.quantity,
            machine=step.machine,
            manpower=step.manpower,
            setup_minutes=step.setup_minutes,
            run_minutes_per_unit=step.run_minutes_per_unit,
            cost_per_unit=step.cost_per_unit,
            allow_out_of_sequence=step.allow_out_of_sequence,
            status="ready" if index == 0 else "not_started",
        ))
    return ops


@transaction.atomic
def create_production_order(recipe, quantity, warehouse, *, sales_order=None, plan_ref="",
                            line=None, planned_start=None, planned_end=None, status="scheduled",
                            plan_approved=True, notes="", materials_reserved=False,
                            rework_of=None, rework_source_lot=None, validate=True):
    """Create a production order from a production requirement (TB-01).

    Track A's Production Plan approval calls this with `plan_ref`; the legacy
    sales-order release calls it with `sales_order`. Returns (order, checks).
    """
    checks = conversion_checks(recipe, quantity, plan_approved=plan_approved) if validate else []
    blocking = [c["message"] for c in checks if c["blocking"] and not c["ok"]]
    if blocking:
        raise ValidationError({"conversion": blocking})
    if line is None and rework_of is None:
        from .planning import default_line_for
        line = default_line_for(recipe)
    order = ProductionOrder.objects.create(
        recipe=recipe,
        quantity=float(quantity),
        warehouse=warehouse,
        line=line,
        sales_order=sales_order,
        production_plan_ref=plan_ref or "",
        planned_start=planned_start,
        planned_end=planned_end,
        status=status,
        notes=notes,
        materials_reserved=materials_reserved,
        rework_of=rework_of,
        rework_source_lot=rework_source_lot,
    )
    build_operations(order)
    return order, checks


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def _stocks(item):
    return list(Stock.objects.filter(item=item, quantity__gt=0).order_by("-quantity"))


def _draw(order, item, quantity, user, reference):
    """Deduct `quantity` of `item` across warehouses and record lot genealogy.
    Returns the quantity actually drawn."""
    from inventory.lots import consume_lots_fifo

    remaining = quantity
    drawn = 0
    for stock in _stocks(item):
        if remaining <= EPS:
            break
        take = min(stock.quantity, remaining)
        if take <= 0:
            continue
        decrease_stock(item, stock.warehouse, take, user=user, reference=reference)
        drawn += take
        remaining -= take
    if drawn > 0:
        consume_lots_fifo(order, item, drawn, company=getattr(item, "company", None))
    return drawn


@transaction.atomic
def reserve_materials(order, user=None, reference=None):
    """Issue what is on hand for the whole order now and record the ledger.

    Returns [(item, shortage_qty), ...]. Never deducts more than is available
    and never more than the requirement.
    """
    reference = reference or (
        f"Reserved for SO#{order.sales_order_id} production" if order.sales_order_id
        else f"Reserved for Production #{order.id}"
    )
    shortages = []
    for ing, required in order.recipe.material_requirements(order.quantity):
        ledger, _ = ProductionMaterialRequirement.objects.get_or_create(
            production_order=order, item=ing.item, defaults={"required_quantity": required},
        )
        ledger.required_quantity = required
        needed = max(required - ledger.consumed_quantity, 0)
        if needed > EPS:
            drawn = _draw(order, ing.item, needed, user, reference)
            ledger.reserved_quantity += drawn
            ledger.issued_quantity += drawn
            ledger.consumed_quantity += drawn
        ledger.shortage_quantity = max(required - ledger.consumed_quantity, 0)
        ledger.save()
        if ledger.shortage_quantity > EPS:
            shortages.append((ing.item, ledger.shortage_quantity))
    fully = not shortages
    if fully != order.materials_reserved:
        order.materials_reserved = fully
        ProductionOrder.objects.filter(pk=order.pk).update(materials_reserved=fully)
    return shortages


def issue_materials(order, cumulative_quantity, user=None):
    """Bring consumption up to what `cumulative_quantity` of output needs.

    Raises ValidationError listing every shortage; the caller's transaction
    rolls back so nothing is half-issued.
    """
    if order.is_rework:
        return
    errors = []
    fraction = min(cumulative_quantity / order.quantity, 1) if order.quantity else 1
    for ing, required in order.recipe.material_requirements(order.quantity):
        ledger, _ = ProductionMaterialRequirement.objects.get_or_create(
            production_order=order, item=ing.item, defaults={"required_quantity": required},
        )
        ledger.required_quantity = required
        target = required * fraction
        additional = target - ledger.consumed_quantity
        if additional <= EPS:
            ledger.shortage_quantity = max(required - ledger.consumed_quantity, 0)
            ledger.save()
            continue
        available = sum(s.quantity for s in _stocks(ing.item))
        if available + EPS < additional:
            short = additional - available
            message = (f"Insufficient stock for {ing.item.name}: need {_fmt(additional)} more "
                       f"(required {_fmt(required)}, already issued {_fmt(ledger.consumed_quantity)}), "
                       f"available {_fmt(available)}")
            if ing.item.category == "intermediate":
                message += f". {ing.item.name} is an intermediate: produce {_fmt(short)} more first."
            ledger.shortage_quantity = max(required - ledger.consumed_quantity, 0)
            ledger.save()
            errors.append(message)
            continue
        drawn = _draw(order, ing.item, additional, user, f"Production #{order.id}")
        ledger.issued_quantity += drawn
        ledger.consumed_quantity += drawn
        ledger.shortage_quantity = max(required - ledger.consumed_quantity, 0)
        ledger.save()
    if errors:
        raise ValidationError(errors)


def material_status(order):
    """'available' | 'partial' | 'short' for the order's remaining requirement."""
    if order.is_rework or order.materials_reserved:
        return "available"
    rows = list(order.material_requirements.all())
    if not rows:
        from .planning import material_check
        return "available" if material_check(order.recipe, order.quantity)["can_produce"] else "short"
    short = [r for r in rows if r.remaining_quantity > EPS]
    if not short:
        return "available"
    stock_ok = all(
        sum(s.quantity for s in _stocks(r.item)) + EPS >= r.remaining_quantity for r in short
    )
    if stock_ok:
        return "available"
    return "partial" if any(r.consumed_quantity > 0 for r in rows) else "short"


def refresh_material_status(order):
    """Move a Material Pending order to Ready once its materials are on hand."""
    if order.status == "material_pending" and material_status(order) == "available":
        order.status = "scheduled"
        order.save(update_fields=["status"])
    return order.status


# ---------------------------------------------------------------------------
# Order lifecycle
# ---------------------------------------------------------------------------

def _require_active(order):
    if order.status in ("cancelled", "closed"):
        raise ValidationError(f"{order} is {order.get_status_display()}; no further work can be reported.")
    if order.status in ("draft", "pending_approval"):
        raise ValidationError(f"{order} is not approved for production yet.")


@transaction.atomic
def approve_order(order, user=None):
    if order.status not in ("draft", "pending_approval"):
        raise ValidationError(f"Only draft or pending orders can be approved; {order} is {order.get_status_display()}.")
    order.status = "scheduled" if material_status(order) == "available" else "material_pending"
    order.save(update_fields=["status"])
    return order


@transaction.atomic
def start_order(order, user=None):
    if order.status == "running":
        return order
    if order.status not in ProductionOrder.STARTABLE_STATUSES:
        raise ValidationError(f"Only ready orders can be started; {order} is {order.get_status_display()}.")
    if order.line_id and order.line.status == "maintenance":
        raise ValidationError(f"Line {order.line.name} is under maintenance.")
    order.status = "running"
    order.start_time = order.start_time or timezone.now()
    order.save(update_fields=["status", "start_time"])
    ops = build_operations(order)
    first = ops[0] if ops else None
    if first and first.status == "not_started":
        first.status = "ready"
        first.save(update_fields=["status"])
    return order


@transaction.atomic
def cancel_order(order, user=None, reason=""):
    """Cancel an order that has produced nothing; issued material goes back to stock."""
    if order.status in ("cancelled", "closed"):
        raise ValidationError(f"{order} is already {order.get_status_display()}.")
    if order.produced_quantity > EPS:
        raise ValidationError("Output has already been reported; close the order instead of cancelling it.")
    for ledger in order.material_requirements.select_related("item"):
        if ledger.consumed_quantity > EPS:
            increase_stock(ledger.item, order.warehouse, ledger.consumed_quantity, user=user,
                           reference=f"Returned from cancelled Production #{order.id}")
            ledger.consumed_quantity = ledger.issued_quantity = ledger.reserved_quantity = 0
            ledger.shortage_quantity = 0
            ledger.save()
    order.status = "cancelled"
    order.notes = (order.notes + f"\nCancelled: {reason}").strip() if reason else order.notes
    order.save(update_fields=["status", "notes"])
    order.inventoryrequest_set.filter(status__in=["pending", "procuring"]).update(status="cancelled")
    return order


@transaction.atomic
def close_order(order, user=None):
    """Close an order. A partially produced order is short-closed: the
    remaining quantity will not be produced."""
    if order.status in ("cancelled", "closed"):
        raise ValidationError(f"{order} is already {order.get_status_display()}.")
    if order.produced_quantity <= EPS:
        raise ValidationError("Nothing has been produced; cancel the order instead.")
    pending = order.qualitycheck_set.filter(status="pending").exists()
    if pending:
        raise ValidationError("QA is still pending on this order's output.")
    order.status = "closed"
    order.end_time = order.end_time or timezone.now()
    order.save(update_fields=["status", "end_time"])
    order.inventoryrequest_set.filter(status__in=["pending", "procuring"]).update(status="cancelled")
    from quality.services import maybe_post_manufacturing_accounting
    maybe_post_manufacturing_accounting(order, user)
    return order


# ---------------------------------------------------------------------------
# Shop-floor operations (TB-03)
# ---------------------------------------------------------------------------

def _ops(order):
    return list(order.operations.order_by("sequence"))


def _previous(op):
    return [o for o in _ops(op.production_order) if o.sequence < op.sequence]


def _enforced(op):
    if op.allow_out_of_sequence:
        return False
    return ManufacturingSettings.for_company(_company(op.production_order)).enforce_operation_sequence


def _input_quantity(op):
    """Units that have reached this operation and may be processed here."""
    previous = _previous(op)
    if not previous or not _enforced(op):
        return op.planned_quantity
    return previous[-1].completed_quantity


def _processed(op):
    return op.completed_quantity + op.rejected_quantity + op.scrap_quantity + op.rework_quantity


def resource_issues(resource, start=None, end=None, kind=None):
    """Why `resource` cannot take work in [start, end] (TB-07). Empty list = available.

    Busy is not an issue (load is a capacity question, see capacity.py); a
    resource that is inactive, in maintenance, unavailable, whose machine is
    broken down / in maintenance, or that is booked off in the window is.
    """
    issues = []
    if resource is None:
        return issues
    if kind and resource.resource_type != kind:
        issues.append(f"{resource.name} is a {resource.get_resource_type_display().lower()} resource, not {kind}.")
    if not resource.is_active:
        issues.append(f"{resource.name} is inactive.")
    if resource.status in ("maintenance", "unavailable"):
        issues.append(f"{resource.name} is {resource.get_status_display().lower()}.")
    if resource.equipment_id and resource.equipment.status in ("maintenance", "breakdown"):
        issues.append(f"{resource.name}: machine {resource.equipment.name} is in {resource.equipment.status}.")
    start = start or timezone.now()
    end = end or start
    clash = resource.unavailability.filter(start__lte=end, end__gte=start).first()
    if clash:
        issues.append(
            f"{resource.name} is unavailable {timezone.localtime(clash.start):%Y-%m-%d %H:%M} to "
            f"{timezone.localtime(clash.end):%Y-%m-%d %H:%M}" + (f" ({clash.reason})" if clash.reason else "") + "."
        )
    return issues


def check_assignment(op, machine=None, manpower=None):
    """Raise if a resource assigned to `op` cannot work in the operation's window."""
    start = op.planned_start or op.production_order.planned_start
    end = op.planned_end or op.production_order.planned_end
    issues = resource_issues(machine, start, end, "machine") + resource_issues(manpower, start, end, "manpower")
    if issues:
        raise ValidationError(issues)


@transaction.atomic
def start_operation(op, user=None, quantity=None, machine=None, manpower=None, operator=None):
    order = op.production_order
    _require_active(order)
    if op.status == "completed":
        raise ValidationError(f"Operation {op.sequence} ({op.name}) is already completed.")
    if order.status != "running":
        start_order(order, user)
    if _enforced(op):
        previous = _previous(op)
        if previous and previous[-1].completed_quantity <= EPS and previous[-1].status != "completed":
            raise ValidationError(
                f"Operation {previous[-1].sequence} ({previous[-1].name}) has not completed any units yet."
            )
    if machine is not None:
        op.machine = machine
    if manpower is not None:
        op.manpower = manpower
    if operator is not None:
        op.operator = operator
    # Work starts now: every assigned resource must be available now.
    issues = resource_issues(op.machine, kind="machine") + resource_issues(op.manpower, kind="manpower")
    if issues:
        raise ValidationError(issues)
    qty = float(quantity) if quantity not in (None, "") else max(_input_quantity(op) - op.started_quantity, 0)
    op.started_quantity = min(op.started_quantity + qty, op.planned_quantity)
    op.status = "in_progress"
    op.actual_start = op.actual_start or timezone.now()
    op.save()
    _mark_resources(op, "busy")
    return op


def _mark_resources(op, status):
    for resource in (op.machine, op.manpower):
        if resource and resource.status in ("available", "busy"):
            resource.status = status
            resource.save(update_fields=["status"])


@transaction.atomic
def report_operation(op, user=None, good=0, rejected=0, scrap=0, rework=0, minutes=None,
                     notes="", complete=False, scrap_reason=""):
    """Record shop-floor output of one operation."""
    order = op.production_order
    _require_active(order)
    values = {"good": good, "rejected": rejected, "scrap": scrap, "rework": rework}
    try:
        values = {k: float(v or 0) for k, v in values.items()}
    except (TypeError, ValueError):
        raise ValidationError("Quantities must be numbers.")
    if any(v < 0 for v in values.values()):
        raise ValidationError("Quantities cannot be negative.")
    total = sum(values.values())
    if total <= 0 and not complete:
        raise ValidationError("Report at least one unit.")
    if op.status == "completed" and total > 0:
        raise ValidationError(f"Operation {op.sequence} ({op.name}) is already completed.")
    if op.status in ("not_started", "ready", "paused") and total > 0:
        start_operation(op, user)
        op.refresh_from_db()

    limit = _input_quantity(op)
    if _processed(op) + total > limit + EPS:
        if _enforced(op) and _previous(op):
            prev = _previous(op)[-1]
            raise ValidationError(
                f"Only {_fmt(limit)} unit(s) have completed Operation {prev.sequence} ({prev.name}); "
                f"{_fmt(_processed(op))} already processed here."
            )
        raise ValidationError(
            f"Cannot process {_fmt(total)} more: planned {_fmt(op.planned_quantity)}, "
            f"already processed {_fmt(_processed(op))}."
        )

    op.completed_quantity += values["good"]
    op.rejected_quantity += values["rejected"]
    op.scrap_quantity += values["scrap"]
    op.rework_quantity += values["rework"]
    if minutes not in (None, ""):
        op.actual_minutes = (op.actual_minutes or 0) + float(minutes)
    if notes:
        op.notes = (op.notes + "\n" + notes).strip()

    finished = _processed(op) >= op.planned_quantity - EPS
    if complete or finished:
        unfinished_prev = [p for p in _previous(op) if p.status != "completed"] if _enforced(op) else []
        if unfinished_prev:
            if complete:
                p = unfinished_prev[-1]
                raise ValidationError(
                    f"Operation {op.sequence} cannot be completed before Operation {p.sequence} ({p.name})."
                )
        else:
            op.status = "completed"
            op.actual_end = timezone.now()
    if values["rework"] > 0 and op.status != "completed":
        op.status = "rework_required"
    op.save()

    if values["scrap"] > 0:
        record_scrap(order, order.recipe.product, values["scrap"], user=user, operation=op,
                     reason=scrap_reason or "Scrapped at operation", resource=op.machine or op.manpower)

    if op.status == "completed":
        _mark_resources(op, "available")
        nxt = [o for o in _ops(order) if o.sequence > op.sequence and o.status == "not_started"]
        if nxt:
            nxt[0].status = "ready"
            nxt[0].save(update_fields=["status"])
    elif op.status == "in_progress":
        nxt = [o for o in _ops(order) if o.sequence > op.sequence and o.status == "not_started"]
        if nxt and op.completed_quantity > 0:
            nxt[0].status = "ready"
            nxt[0].save(update_fields=["status"])
    return op


@transaction.atomic
def set_operation_status(op, status, user=None, notes=""):
    allowed = {"paused", "in_progress", "failed", "rework_required"}
    if status not in allowed:
        raise ValidationError(f"Status must be one of {sorted(allowed)}.")
    if op.status == "completed":
        raise ValidationError("A completed operation cannot change status.")
    if status == "in_progress" and op.status not in ("paused", "rework_required", "failed"):
        return start_operation(op, user)
    op.status = status
    if notes:
        op.notes = (op.notes + "\n" + notes).strip()
    op.save(update_fields=["status", "notes"])
    if status in ("paused", "failed"):
        _mark_resources(op, "available")
    return op


# ---------------------------------------------------------------------------
# Output (partial production) and QA hand-off
# ---------------------------------------------------------------------------

@transaction.atomic
def report_output(order, quantity, user=None):
    """Report `quantity` finished units: issue materials, create a QA-pending
    lot and a QA check. Supports any number of partial outputs."""
    from inventory.lots import create_finished_lot
    from quality.models import QualityCheck
    from quality.services import refresh_order_qa_status

    order = ProductionOrder.objects.select_for_update().get(pk=order.pk)
    _require_active(order)
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Quantity must be a number.")
    if quantity <= 0:
        raise ValidationError("Output quantity must be greater than zero.")
    if order.produced_quantity + quantity > order.quantity + EPS:
        raise ValidationError(
            f"Cannot report {_fmt(quantity)}: planned {_fmt(order.quantity)}, "
            f"already produced {_fmt(order.produced_quantity)} (remaining {_fmt(order.remaining_quantity)})."
        )
    if order.status in ProductionOrder.STARTABLE_STATUSES:
        start_order(order, user)

    ops = build_operations(order)
    final = ops[-1]
    cumulative = order.produced_quantity + quantity
    if final.is_default or (order.is_rework and len(ops) == 1):
        # No routing: the single operation advances with the reported output.
        final.refresh_from_db()
        final.actual_start = final.actual_start or timezone.now()
        final.started_quantity = max(final.started_quantity, cumulative)
        final.completed_quantity = max(final.completed_quantity, cumulative)
        if final.completed_quantity >= final.planned_quantity - EPS:
            final.status = "completed"
            final.actual_end = timezone.now()
        else:
            final.status = "in_progress"
        final.save()
    elif final.completed_quantity + EPS < cumulative:
        raise ValidationError(
            f"Only {_fmt(final.completed_quantity)} unit(s) have completed the final operation "
            f"({final.name}); {_fmt(order.produced_quantity)} already reported as output."
        )

    issue_materials(order, cumulative, user)

    product = order.recipe.product
    # Sub-products (semi-finished goods) are consumed in-house by the finished
    # good, so they skip QA: the output is booked straight into inventory and is
    # immediately available to the finished-good work order. Finished goods (and
    # rework) still go through the QA gate before stock is released.
    skip_qa = product.is_semi_finished and not order.is_rework

    lot = create_finished_lot(product, order.warehouse, quantity, order, company=getattr(product, "company", None))
    if skip_qa:
        lot.remaining_quantity = quantity
        lot.qa_status = "not_required"
    else:
        lot.remaining_quantity = 0
        lot.qa_status = "pending"
    if order.rework_source_lot_id:
        lot.parent_id = order.rework_source_lot_id
    lot.save(update_fields=["remaining_quantity", "qa_status", "parent"])

    output = ProductionOutput.objects.create(
        production_order=order, quantity=quantity, lot=lot,
        reported_by=user if getattr(user, "is_authenticated", False) else None,
    )
    if skip_qa:
        increase_stock(product, order.warehouse, quantity, user=user,
                       reference=f"Sub-product output, Production #{order.id} (no QA)")
    else:
        QualityCheck.objects.create(
            inspection_type="production",
            production_order=order,
            output=output,
            lot=lot,
            item=product,
            warehouse=order.warehouse,
            received_quantity=quantity,
            status="pending",
            test_type="Rework Inspection" if order.is_rework else "Post-Production",
            parameter="Visual & Weight",
        )

    order.produced_quantity = cumulative
    if order.remaining_quantity <= EPS:
        order.status = "completed"
        order.end_time = timezone.now()
    else:
        order.status = "partially_completed"
    order.save()
    if skip_qa:
        # No QA for sub-products: mark the order as passing so it isn't left
        # waiting on an inspection that will never be raised.
        if order.qa_status != "passed":
            order.qa_status = "passed"
            type(order).objects.filter(pk=order.pk).update(qa_status="passed")
    else:
        refresh_order_qa_status(order)

    from core.utils import send_notification
    if skip_qa:
        send_notification(
            "production",
            f"{order}: {_fmt(quantity)} {product.unit} of sub-product {product.name} added to inventory "
            f"(lot {lot.batch_number}) — ready for the finished product.",
            related_id=order.id, related_type="ProductionOrder",
            company=getattr(product, "company", None), module="production",
        )
    else:
        send_notification(
            "quality",
            f"{order}: {_fmt(quantity)} {product.unit} of {product.name} reported and awaiting QA (lot {lot.batch_number}).",
            related_id=order.id, related_type="ProductionOrder",
            company=getattr(product, "company", None), module="quality",
        )
    return output


def complete_order(order, user=None):
    """Report the whole remaining quantity as output (legacy 'complete')."""
    if order.status == "completed" or order.remaining_quantity <= EPS:
        raise ValidationError("This production order is already completed.")
    return report_output(order, order.remaining_quantity, user)


# ---------------------------------------------------------------------------
# Scrap and rework (TB-18)
# ---------------------------------------------------------------------------

def unit_cost_of(item, company):
    from accounting.manufacturing_accounting import calculate_item_unit_cost
    try:
        return Decimal(str(calculate_item_unit_cost(item, company)))
    except Exception:
        return Decimal("0")


@transaction.atomic
def record_scrap(order, item, quantity, user=None, operation=None, reason="", resource=None,
                 lot=None, source="operation"):
    quantity = float(quantity)
    if quantity <= 0:
        raise ValidationError("Scrap quantity must be greater than zero.")
    company = _company(order)
    if item == order.recipe.product:
        from .costing import order_unit_cost
        # Reworked units carry the cost of the order that first made them.
        unit_cost = order_unit_cost(order.rework_of if order.is_rework else order)
    else:
        unit_cost = unit_cost_of(item, company)
        # Scrapped raw material leaves stock.
        available = sum(s.quantity for s in _stocks(item))
        if available + EPS < quantity:
            raise ValidationError(f"Only {_fmt(available)} of {item.name} in stock to scrap.")
        _draw(order, item, quantity, user, f"Scrap Production #{order.id}")
    if item != order.recipe.product:
        gl_treatment = "raw_material"
    elif order.is_rework or (lot is not None and lot.qa_status == "quarantine"):
        # Rework and quarantined units were valued into finished goods.
        gl_treatment = "finished_goods"
    else:
        gl_treatment = "covered"
    if lot is not None:
        lot.qa_status = "scrapped"
        lot.remaining_quantity = 0
        lot.save(update_fields=["qa_status", "remaining_quantity"])
    record = ScrapRecord.objects.create(
        production_order=order,
        operation=operation,
        item=item,
        lot=lot,
        quantity=quantity,
        reason=reason,
        resource=resource,
        source=source,
        unit_cost=unit_cost,
        cost_impact=(unit_cost * Decimal(str(quantity))).quantize(Decimal("0.01")),
        recorded_by=user if getattr(user, "is_authenticated", False) else None,
        gl_treatment=gl_treatment,
    )
    if gl_treatment != "covered" and record.cost_impact > 0:
        from accounting.auto_posting import queue_auto_post
        queue_auto_post("scrap_write_off", company, record.id, user)
    return record


@transaction.atomic
def create_rework_order(lot, user=None, quantity=None):
    """Send a QA 'rework' lot back to the shop floor. The rework order keeps the
    original production order and lot as its source (QA Failed → Rework →
    Operation → QA again)."""
    if lot.qa_status != "rework":
        raise ValidationError("Only lots marked for rework can be reworked.")
    qty = float(quantity) if quantity else (lot.remaining_quantity or lot.quantity)
    if qty <= 0 or qty > (lot.remaining_quantity or 0) + EPS:
        raise ValidationError(f"Rework quantity must be between 0 and {_fmt(lot.remaining_quantity or 0)}.")
    original = lot.production_order
    if original is None:
        raise ValidationError("This lot is not linked to a production order.")
    order, _ = create_production_order(
        original.recipe, qty, original.warehouse,
        sales_order=original.sales_order,
        plan_ref=original.production_plan_ref,
        line=original.line,
        status="scheduled",
        materials_reserved=True,
        rework_of=original,
        rework_source_lot=lot,
        notes=f"Rework of lot {lot.batch_number} from {original}",
        validate=False,
    )
    lot.remaining_quantity = max((lot.remaining_quantity or 0) - qty, 0)
    lot.save(update_fields=["remaining_quantity"])
    return order


def order_summary(order):
    """Quantities used by the WIP board, traceability and documents."""
    ops = _ops(order)
    agg = order.qualitycheck_set.aggregate(
        accepted=Sum("accepted_quantity"), rejected=Sum("rejected_quantity"),
        quarantined=Sum("quarantined_quantity"), rework=Sum("rework_quantity"),
    )
    pending = sum(qc.pending_quantity for qc in order.qualitycheck_set.filter(status="pending"))
    return {
        "planned": order.quantity,
        "produced": order.produced_quantity,
        "remaining": order.remaining_quantity,
        "qa_pending": pending,
        "qa_accepted": agg["accepted"] or 0,
        "qa_rejected": agg["rejected"] or 0,
        "qa_quarantined": agg["quarantined"] or 0,
        "qa_rework": agg["rework"] or 0,
        "op_rejected": sum(o.rejected_quantity for o in ops),
        "op_rework": sum(o.rework_quantity for o in ops),
        "op_scrap": sum(o.scrap_quantity for o in ops),
    }
