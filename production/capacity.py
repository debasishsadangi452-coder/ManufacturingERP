"""Machine and manpower capacity planning (Track B, TB-08 / TB-19).

Formulas (parameters come from Resource / ManufacturingSettings, see TB-19):

  Available hours  = hours_per_shift x shifts_per_day x working days in window
                     x units  - unavailable hours (resource unavailability and
                     open maintenance tasks on the linked equipment)
  Load hours       = remaining minutes of every open operation assigned to the
                     resource / 60, where remaining minutes =
                     setup (if not started) + run minutes per unit x units left
  Utilisation %    = load / available x 100
  Overloaded       = utilisation > overload threshold
  Expected delay   = (load - available) / daily available hours, in days
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import ManufacturingSettings, ProductionOperation, ProductionOrder, Resource

OPEN_OP_STATUSES = ("not_started", "ready", "in_progress", "paused", "rework_required")
OPEN_ORDER_STATUSES = ProductionOrder.ACTIVE_STATUSES


def _window(start=None, end=None, days=None):
    today = timezone.localdate()
    start = start or today
    end = end or (start + timedelta(days=(days or 7) - 1))
    if end < start:
        start, end = end, start
    return start, end


def _working_days(start, end, days_per_week):
    total_days = (end - start).days + 1
    return total_days * min(max(days_per_week, 0), 7) / 7


def _overlap_hours(a_start, a_end, b_start, b_end):
    latest = max(a_start, b_start)
    earliest = min(a_end, b_end)
    return max((earliest - latest).total_seconds() / 3600, 0)


def _unavailable_hours(resource, start, end, daily_hours):
    tz = timezone.get_current_timezone()
    w_start = timezone.make_aware(datetime.combine(start, time.min), tz)
    w_end = timezone.make_aware(datetime.combine(end, time.max), tz)
    hours = 0.0
    for period in resource.unavailability.filter(end__gte=w_start, start__lte=w_end):
        # Clip each calendar period to working hours: at most one working day per day.
        span = _overlap_hours(period.start, period.end, w_start, w_end)
        hours += min(span, daily_hours * max(span / 24, 1))
    if resource.equipment_id:
        from maintenance.models import MaintenanceTask
        for task in MaintenanceTask.objects.filter(
            equipment_id=resource.equipment_id,
            status__in=["scheduled", "in-progress"],
            scheduled_date__date__gte=start, scheduled_date__date__lte=end,
        ):
            hours += _duration_hours(task.estimated_duration, daily_hours)
    return hours


def _duration_hours(text, default):
    """'4 hours', '2h', '30 min' -> hours. Unknown formats count as one shift."""
    if not text:
        return default
    import re
    match = re.match(r"\s*([\d.]+)\s*(m|min|minute)?", str(text).lower())
    if not match:
        return default
    value = float(match.group(1))
    return value / 60 if match.group(2) else value


def resource_capacity(resource, start, end, settings=None):
    settings = settings or ManufacturingSettings.for_company(resource.company)
    cal = resource.calendar(settings)
    daily = cal["hours_per_shift"] * cal["shifts_per_day"] * resource.units
    days = _working_days(start, end, cal["working_days_per_week"])
    gross = daily * days
    unavailable = _unavailable_hours(resource, start, end, daily)
    if resource.status in ("maintenance", "unavailable") or not resource.is_active:
        unavailable = gross
    available = max(gross - unavailable, 0)

    field = "machine" if resource.resource_type == "machine" else "manpower"
    ops = ProductionOperation.objects.filter(
        **{field: resource},
        status__in=OPEN_OP_STATUSES,
        production_order__status__in=OPEN_ORDER_STATUSES,
    ).select_related("production_order__recipe__product")
    assignments = []
    load = 0.0
    for op in ops:
        planned_date = (op.planned_start or op.production_order.planned_start)
        if planned_date and not (start <= timezone.localtime(planned_date).date() <= end):
            continue
        hours = op.remaining_minutes / 60
        load += hours
        assignments.append({
            "operation_id": op.id,
            "production_order": op.production_order.order_number,
            "production_order_id": op.production_order_id,
            "operation": f"Op {op.sequence} {op.name}",
            "product": op.production_order.recipe.product.name,
            "remaining_hours": round(hours, 2),
            "planned_start": op.planned_start or op.production_order.planned_start,
            "planned_end": op.planned_end or op.production_order.planned_end,
        })
    utilisation = (load / available * 100) if available else (100.0 if load else 0.0)
    overloaded = load > 0 and (available == 0 or utilisation > settings.overload_threshold_percent)
    delay_days = ((load - available) / daily) if overloaded and daily else 0
    return {
        "resource_id": resource.id,
        "code": resource.code,
        "name": resource.name,
        "resource_type": resource.resource_type,
        "status": resource.status,
        "hours_per_day": round(daily, 2),
        "gross_hours": round(gross, 2),
        "unavailable_hours": round(min(unavailable, gross), 2),
        "available_hours": round(available, 2),
        "load_hours": round(load, 2),
        "utilisation_percent": round(utilisation, 1),
        "overloaded": overloaded,
        "expected_delay_days": round(delay_days, 2),
        "assignments": assignments,
        "conflicts": _conflicts(assignments),
    }


def _conflicts(assignments):
    """Operations on the same resource whose planned windows overlap."""
    dated = [a for a in assignments if a["planned_start"] and a["planned_end"]]
    dated.sort(key=lambda a: a["planned_start"])
    found = []
    for i, a in enumerate(dated):
        for b in dated[i + 1:]:
            if b["planned_start"] < a["planned_end"]:
                found.append(f"{a['production_order']} {a['operation']} overlaps {b['production_order']} {b['operation']}")
    return found


def capacity_plan(company, start=None, end=None, days=None):
    start, end = _window(start, end, days)
    settings = ManufacturingSettings.for_company(company)
    rows = [resource_capacity(r, start, end, settings)
            for r in Resource.objects.filter(company=company, is_active=True)]
    unassigned = ProductionOperation.objects.filter(
        production_order__recipe__product__company=company,
        production_order__status__in=OPEN_ORDER_STATUSES,
        status__in=OPEN_OP_STATUSES,
        manpower__isnull=True,
        is_default=False,
    ).select_related("production_order")
    manpower_gaps = [
        f"{op.production_order.order_number} Op {op.sequence} {op.name} has no manpower assigned"
        for op in unassigned
    ]
    machines = [r for r in rows if r["resource_type"] == "machine"]
    people = [r for r in rows if r["resource_type"] == "manpower"]

    def _sum(group, key):
        return round(sum(r[key] for r in group), 2)

    return {
        "window": {"start": start, "end": end},
        "formula": __doc__.strip(),
        "settings": {
            "default_hours_per_shift": settings.default_hours_per_shift,
            "default_shifts_per_day": settings.default_shifts_per_day,
            "default_working_days_per_week": settings.default_working_days_per_week,
            "overload_threshold_percent": settings.overload_threshold_percent,
        },
        "machines": machines,
        "manpower": people,
        "summary": {
            "machine_available_hours": _sum(machines, "available_hours"),
            "machine_load_hours": _sum(machines, "load_hours"),
            "manpower_available_hours": _sum(people, "available_hours"),
            "manpower_load_hours": _sum(people, "load_hours"),
            "overloaded_resources": [r["name"] for r in rows if r["overloaded"]],
            "manpower_shortages": manpower_gaps,
            "scheduling_conflicts": [c for r in rows for c in r["conflicts"]],
            "max_expected_delay_days": max([r["expected_delay_days"] for r in rows] or [0]),
        },
    }


def order_capacity_check(order, days=14):
    """Can the resources on this order's operations absorb its remaining load?"""
    start = timezone.localdate()
    if order.planned_start:
        start = timezone.localtime(order.planned_start).date()
    end = start + timedelta(days=days - 1)
    if order.planned_end:
        end = max(timezone.localtime(order.planned_end).date(), start)
    settings = ManufacturingSettings.for_company(order.recipe.product.company)
    seen, rows, issues = set(), [], []
    for op in order.operations.select_related("machine", "manpower"):
        if not op.machine_id and not op.manpower_id and not op.is_default:
            issues.append(f"Op {op.sequence} {op.name} has no resource assigned.")
        for res in (op.machine, op.manpower):
            if res and res.id not in seen:
                seen.add(res.id)
                row = resource_capacity(res, start, end, settings)
                rows.append(row)
                if row["overloaded"]:
                    issues.append(
                        f"{res.name} is overloaded ({row['utilisation_percent']}% of available hours); "
                        f"expected delay {row['expected_delay_days']} day(s)."
                    )
                if res.status in ("maintenance", "unavailable"):
                    issues.append(f"{res.name} is {res.get_status_display().lower()}.")
    return {"window": {"start": start, "end": end}, "resources": rows, "issues": issues, "ok": not issues}
