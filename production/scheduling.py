"""Finite-capacity forward scheduling (Track B, TB-08).

Each open operation of an order is placed, in routing sequence, into the
earliest time in which every resource it uses is

  * working     — inside its shift calendar (shift start, hours per shift x
                  shifts per day, working days per week; ManufacturingSettings
                  defaults when the resource sets none),
  * free        — not booked by another order's scheduled operation,
  * available   — not in a ResourceUnavailability period or scheduled
                  maintenance of its linked equipment.

An operation may span several working windows (it pauses overnight). Its
duration is its remaining minutes (setup if not started + run minutes x units
left) divided by the resource's `units` (identical machines / people working in
parallel). An operation never starts before the previous one ends.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ManufacturingSettings, ProductionOperation, ProductionOrder

HORIZON_DAYS = 365
OPEN_OP = ("not_started", "ready", "in_progress", "paused", "rework_required")
EPS = timedelta(seconds=1)


def _calendar(resource, settings):
    if resource is None:
        return {
            "hours_per_shift": settings.default_hours_per_shift,
            "shifts_per_day": settings.default_shifts_per_day,
            "working_days_per_week": settings.default_working_days_per_week,
        }
    return resource.calendar(settings)


def _working_day(day, cal):
    # Mon=0 … Sun=6; 6 working days = Mon–Sat, 5 = Mon–Fri, 7 = every day.
    return day.weekday() < round(min(max(cal["working_days_per_week"], 0), 7))


def _window(day, cal, settings):
    """The working window of `day` as (start, end), or None."""
    if not _working_day(day, cal):
        return None
    hours = cal["hours_per_shift"] * cal["shifts_per_day"]
    if hours <= 0:
        return None
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, settings.shift_start_time or time(8, 0)), tz)
    return start, start + timedelta(hours=min(hours, 24))


def _busy(resource, exclude_order_id):
    """Booked and unavailable intervals of a resource, sorted."""
    if resource is None:
        return []
    intervals = []
    field = "machine" if resource.resource_type == "machine" else "manpower"
    for op in ProductionOperation.objects.filter(
        **{field: resource}, status__in=OPEN_OP,
        planned_start__isnull=False, planned_end__isnull=False,
        production_order__status__in=ProductionOrder.ACTIVE_STATUSES,
    ).exclude(production_order_id=exclude_order_id):
        intervals.append((op.planned_start, op.planned_end))
    for period in resource.unavailability.all():
        intervals.append((period.start, period.end))
    if resource.equipment_id:
        from maintenance.models import MaintenanceTask
        from .capacity import _duration_hours
        for task in MaintenanceTask.objects.filter(
            equipment_id=resource.equipment_id, status__in=["scheduled", "in-progress"],
            scheduled_date__isnull=False,
        ):
            intervals.append((task.scheduled_date,
                              task.scheduled_date + timedelta(hours=_duration_hours(task.estimated_duration, 4))))
    return sorted(intervals)


def _free_segments(day, resources, settings, busy):
    """Free (start, end) segments on `day` common to every resource."""
    segments = None
    for res in resources or [None]:
        cal = _calendar(res, settings)
        window = _window(day, cal, settings)
        if window is None:
            return []
        free = [window]
        for b_start, b_end in busy.get(res.id if res else None, []):
            nxt = []
            for f_start, f_end in free:
                if b_end <= f_start or b_start >= f_end:
                    nxt.append((f_start, f_end))
                    continue
                if b_start > f_start:
                    nxt.append((f_start, b_start))
                if b_end < f_end:
                    nxt.append((b_end, f_end))
            free = nxt
        if segments is None:
            segments = free
        else:
            merged = []
            for a_start, a_end in segments:
                for b_start, b_end in free:
                    s, e = max(a_start, b_start), min(a_end, b_end)
                    if e - s > EPS:
                        merged.append((s, e))
            segments = merged
    return sorted(segments or [])


def _place(resources, not_before, hours, settings, busy):
    """Consume `hours` of common free time starting at `not_before`.
    Returns (start, end)."""
    remaining = timedelta(hours=hours)
    if remaining <= EPS:
        return not_before, not_before
    start = None
    day = timezone.localtime(not_before).date()
    for _ in range(HORIZON_DAYS):
        for seg_start, seg_end in _free_segments(day, resources, settings, busy):
            seg_start = max(seg_start, not_before)
            if seg_end - seg_start <= EPS:
                continue
            if start is None:
                start = seg_start
            take = min(remaining, seg_end - seg_start)
            remaining -= take
            if remaining <= EPS:
                return start, seg_start + take
        day += timedelta(days=1)
    names = ", ".join(r.name for r in resources) or "the default calendar"
    raise ValidationError(f"No capacity on {names} within {HORIZON_DAYS} days.")


def _units(resources):
    return max(min((r.units or 1) for r in resources), 1) if resources else 1


@transaction.atomic
def schedule_order(order, start=None, commit=True):
    """Forward-schedule the open operations of `order` from `start` (default:
    now, or the order's planned start if later)."""
    from .execution import build_operations

    if order.status not in ProductionOrder.ACTIVE_STATUSES:
        raise ValidationError(f"{order} is {order.get_status_display()} and cannot be scheduled.")
    settings = ManufacturingSettings.for_company(order.recipe.product.company)
    now = timezone.now()
    cursor = start or max(now, order.planned_start or now)
    ops = [o for o in build_operations(order) if o.status in OPEN_OP]
    busy = {}
    rows = []
    for op in ops:
        resources = [r for r in (op.machine, op.manpower) if r is not None]
        for r in resources:
            if r.id not in busy:
                busy[r.id] = _busy(r, order.id)
        hours = op.remaining_minutes / 60 / _units(resources)
        op_start, op_end = _place(resources, cursor, hours, settings, busy)
        if op.status == "in_progress" and op.actual_start:
            op_start = min(op_start, op.actual_start)
        rows.append({"operation_id": op.id, "sequence": op.sequence, "name": op.name,
                     "resources": [r.name for r in resources], "hours": round(hours, 2),
                     "planned_start": op_start, "planned_end": op_end})
        for r in resources:  # later operations of this order must not overlap it either
            busy[r.id] = sorted(busy[r.id] + [(op_start, op_end)])
        if commit:
            op.planned_start, op.planned_end = op_start, op_end
            op.save(update_fields=["planned_start", "planned_end"])
        cursor = op_end
    first = rows[0]["planned_start"] if rows else order.planned_start
    last = rows[-1]["planned_end"] if rows else order.planned_end
    if commit and rows:
        order.planned_start = order.planned_start if order.start_time else first
        order.planned_end = last
        order.save(update_fields=["planned_start", "planned_end"])
    return {"production_order": order.id, "order_number": order.order_number,
            "planned_start": first, "planned_end": last, "operations": rows}


@transaction.atomic
def schedule_all(company, start=None, commit=True):
    """Reschedule every open order: running orders first, then by planned end
    (due date) and creation, each booking capacity before the next."""
    orders = list(
        ProductionOrder.objects.filter(recipe__product__company=company, status__in=ProductionOrder.ACTIVE_STATUSES)
        .select_related("recipe__product")
    )
    far = timezone.now() + timedelta(days=10 * HORIZON_DAYS)
    orders.sort(key=lambda o: (o.status != "running", o.planned_end or far, o.created_at))
    # Clear the open operations' bookings so the plan is rebuilt from scratch.
    if commit:
        ProductionOperation.objects.filter(production_order__in=orders, status__in=OPEN_OP).update(
            planned_start=None, planned_end=None)
    results = []
    for order in orders:
        try:
            results.append(schedule_order(order, start=start, commit=commit))
        except ValidationError as e:
            results.append({"production_order": order.id, "order_number": order.order_number,
                            "error": " ".join(str(x) for x in (e.detail if isinstance(e.detail, list) else [e.detail]))})
    return results
