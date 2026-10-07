import logging

from django.db import transaction
from datetime import timedelta

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView
from django.utils import timezone

from accounts.permission import IsAdmin, IsProduction, IsQuality, IsSales, IsStore
from .models import (
    ManufacturingSettings, ProductionLine, ProductionOperation, ProductionOrder, Recipe,
    RecipeIngredient, Resource, ResourceUnavailability, RoutingStep, ScrapRecord,
)
from .serializers import (
    ManufacturingSettingsSerializer,
    ProductionLineSerializer,
    ProductionMaterialRequirementSerializer,
    ProductionOperationSerializer,
    ProductionOrderSerializer,
    ProductionOutputSerializer,
    RecipeIngredientSerializer,
    RecipeSerializer,
    ResourceSerializer,
    ResourceUnavailabilitySerializer,
    RoutingStepSerializer,
    ScrapRecordSerializer,
)
from . import execution
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin

class ProductionLineViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = ProductionLine.objects.all()
    serializer_class = ProductionLineSerializer
    permission_classes = [IsProduction | IsAdmin]


class ProjectionDashboardAPIView(APIView):
    permission_classes = [IsProduction | IsAdmin]

    def get(self, request):
        base_date = timezone.now().date()
        manufacturing_forecast = []
        maintenance_forecast = []
        quality_forecast = []

        manufacturing_values = [
            {"date": (base_date + timedelta(days=1)).isoformat(), "Cola 200": 540, "Cola 500": 370, "Cola 1L": 245},
            {"date": (base_date + timedelta(days=2)).isoformat(), "Cola 200": 552, "Cola 500": 378, "Cola 1L": 251},
            {"date": (base_date + timedelta(days=3)).isoformat(), "Cola 200": 561, "Cola 500": 386, "Cola 1L": 258},
            {"date": (base_date + timedelta(days=4)).isoformat(), "Cola 200": 575, "Cola 500": 392, "Cola 1L": 264},
            {"date": (base_date + timedelta(days=5)).isoformat(), "Cola 200": 589, "Cola 500": 401, "Cola 1L": 271},
            {"date": (base_date + timedelta(days=6)).isoformat(), "Cola 200": 596, "Cola 500": 409, "Cola 1L": 276},
            {"date": (base_date + timedelta(days=7)).isoformat(), "Cola 200": 608, "Cola 500": 417, "Cola 1L": 282},
        ]
        manufacturing_forecast.extend(manufacturing_values)

        maintenance_values = [
            {"date": (base_date + timedelta(days=1)).isoformat(), "risk_score": 22, "scheduled_machines": 1},
            {"date": (base_date + timedelta(days=2)).isoformat(), "risk_score": 26, "scheduled_machines": 1},
            {"date": (base_date + timedelta(days=3)).isoformat(), "risk_score": 31, "scheduled_machines": 2},
            {"date": (base_date + timedelta(days=4)).isoformat(), "risk_score": 35, "scheduled_machines": 2},
            {"date": (base_date + timedelta(days=5)).isoformat(), "risk_score": 33, "scheduled_machines": 1},
            {"date": (base_date + timedelta(days=6)).isoformat(), "risk_score": 29, "scheduled_machines": 1},
            {"date": (base_date + timedelta(days=7)).isoformat(), "risk_score": 27, "scheduled_machines": 1},
        ]
        maintenance_forecast.extend(maintenance_values)

        quality_values = [
            {"date": (base_date + timedelta(days=1)).isoformat(), "pass_rate": 96.8, "flagged_batches": 1},
            {"date": (base_date + timedelta(days=2)).isoformat(), "pass_rate": 97.2, "flagged_batches": 1},
            {"date": (base_date + timedelta(days=3)).isoformat(), "pass_rate": 96.1, "flagged_batches": 2},
            {"date": (base_date + timedelta(days=4)).isoformat(), "pass_rate": 95.6, "flagged_batches": 2},
            {"date": (base_date + timedelta(days=5)).isoformat(), "pass_rate": 96.9, "flagged_batches": 1},
            {"date": (base_date + timedelta(days=6)).isoformat(), "pass_rate": 97.4, "flagged_batches": 1},
            {"date": (base_date + timedelta(days=7)).isoformat(), "pass_rate": 97.1, "flagged_batches": 1},
        ]
        quality_forecast.extend(quality_values)

        return Response(
            {
                "source": "demo_projection_backend",
                "message": "Dummy backend projection data for manufacturing, maintenance, and quality control.",
                "manufacturing_forecast": manufacturing_forecast,
                "maintenance_forecast": maintenance_forecast,
                "quality_control_forecast": quality_forecast,
                "summary": {
                    "finished_products": ["Cola 200", "Cola 500", "Cola 1L"],
                    "manufacturing_total": sum(item["Cola 200"] + item["Cola 500"] + item["Cola 1L"] for item in manufacturing_forecast),
                    "avg_maintenance_risk": round(sum(item["risk_score"] for item in maintenance_forecast) / len(maintenance_forecast), 2),
                    "avg_quality_pass_rate": round(sum(item["pass_rate"] for item in quality_forecast) / len(quality_forecast), 2),
                },
            },
            status=status.HTTP_200_OK,
        )


# -------------------------------------------------
# 🧾 Recipe ViewSet
# -------------------------------------------------

class RecipeViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "product__company"
    queryset = Recipe.objects.select_related('product').all()
    serializer_class = RecipeSerializer
    permission_classes = [IsProduction | IsAdmin]

    def perform_create(self, serializer):
        recipe = serializer.save()
        log_activity(self.request.user, "Production", "Create Recipe", f"Created recipe for '{recipe.product.name}'")

    def perform_destroy(self, instance):
        log_activity(self.request.user, "Production", "Delete Recipe", f"Deleted recipe for '{instance.product.name}'")
        instance.delete()

    @action(detail=True, methods=["get"])
    def plan_batches(self, request, pk=None):
        """Given ?units=<cases ordered>, return how many batches to make.

        This is the cases→batches auto-calc that replaces the customer's manual
        production-planning spreadsheet.
        """
        recipe = self.get_object()
        try:
            units = float(request.query_params.get("units", 0))
        except (TypeError, ValueError):
            return Response({"error": "units must be a number."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"product": recipe.product.name, **recipe.batches_for(units)})

    @action(detail=True, methods=["get"])
    def material_check(self, request, pk=None):
        """?quantity=<units> -> stock check for one run, including intermediates
        that must be produced first (multi-level BOM)."""
        from .planning import material_check
        recipe = self.get_object()
        try:
            quantity = float(request.query_params.get("quantity", 0))
        except (TypeError, ValueError):
            return Response({"error": "quantity must be a number."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"product": recipe.product.name, "quantity": quantity, **material_check(recipe, quantity)})


# -------------------------------------------------
# 🧪 Recipe Ingredient ViewSet
# -------------------------------------------------

class RecipeIngredientViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "recipe__product__company"
    queryset = RecipeIngredient.objects.all()
    serializer_class = RecipeIngredientSerializer
    permission_classes = [IsProduction | IsAdmin]

    def perform_create(self, serializer):
        ing = serializer.save()
        log_activity(self.request.user, "Production", "Add Recipe Ingredient", f"Added {ing.quantity} x '{ing.item.name}' to recipe for '{ing.recipe.product.name}'")


# -------------------------------------------------
# 🏭 Production Order ViewSet
# -------------------------------------------------

def _error(exc):
    """A DRF ValidationError as a single readable message."""
    detail = getattr(exc, "detail", exc)
    if isinstance(detail, dict):
        parts = []
        for value in detail.values():
            parts.extend(value if isinstance(value, list) else [value])
        detail = parts
    if isinstance(detail, list):
        return " & ".join(dict.fromkeys(str(d) for d in detail))
    return str(detail)


def _bad(exc):
    return Response({"error": _error(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _company(request):
    return getattr(request.user, "company", None)


class ProductionOrderViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "recipe__product__company"
    queryset = ProductionOrder.objects.select_related(
        'recipe__product', 'line', 'warehouse', 'sales_order__customer'
    ).all()
    serializer_class = ProductionOrderSerializer
    permission_classes = [IsProduction | IsAdmin]

    def perform_create(self, serializer):
        line = serializer.validated_data.get('line')
        if line is None:
            # No line chosen: use the recipe's predefined line.
            from .planning import default_line_for
            line = default_line_for(serializer.validated_data['recipe'])
            if line is not None:
                serializer.validated_data['line'] = line
        start_time = serializer.validated_data.get('start_time') or serializer.validated_data.get('planned_start')
        end_time = serializer.validated_data.get('end_time') or serializer.validated_data.get('planned_end')

        if line and start_time and end_time:
            from maintenance.models import MaintenanceTask
            from django.db.models import Q

            # Simple overlap check: Maintenance starts before Batch ends AND Maintenance ends after Batch starts
            maintenance_conflicts = MaintenanceTask.objects.filter(
                equipment__line=line,
                status__in=['scheduled', 'in-progress', 'requested']
            ).filter(
                Q(scheduled_date__range=(start_time, end_time)) |
                Q(started_at__range=(start_time, end_time))
            )

            if maintenance_conflicts.exists():
                conflict = maintenance_conflicts.first()
                time_str = conflict.scheduled_date.strftime('%H:%M')
                raise ValidationError({
                    "line": f"This line has maintenance scheduled for {time_str}. Please choose another time or line."
                })

        if line and line.status == 'maintenance':
            raise ValidationError({"line": "This production line is currently under maintenance and cannot accept new orders."})

        # Manual orders only need a valid quantity; the full conversion checks
        # (BOM, materials, operations, resources) run on /convert/.
        quantity = serializer.validated_data.get('quantity') or 0
        if quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})

        order = serializer.save()
        execution.build_operations(order)
        log_activity(self.request.user, "Production", "Create Production Order", f"Created {order.order_number} for {order.quantity} x '{order.recipe.product.name}'")

    # ---------------------------------------------
    # TB-01: conversion from a production requirement
    # ---------------------------------------------
    @action(detail=False, methods=["post"])
    def conversion_check(self, request):
        recipe = Recipe.objects.filter(pk=request.data.get("recipe"), product__company=_company(request)).first()
        if recipe is None:
            return Response({"error": "Recipe not found."}, status=status.HTTP_400_BAD_REQUEST)
        checks = execution.conversion_checks(
            recipe, request.data.get("quantity"), plan_approved=request.data.get("plan_approved", True) is not False
        )
        return Response({"checks": checks, "can_convert": all(c["ok"] for c in checks if c["blocking"])})

    @action(detail=False, methods=["post"])
    def convert(self, request):
        """Create a production order from a planned production requirement.

        Payload: recipe, quantity, warehouse, optional sales_order,
        production_plan_ref, planned_start, planned_end, line, plan_approved,
        reserve_materials (default true).
        """
        from django.utils.dateparse import parse_datetime
        from inventory.models import Warehouse
        from sales.models import SalesOrder
        company = _company(request)
        data = request.data
        recipe = Recipe.objects.filter(pk=data.get("recipe"), product__company=company).first()
        warehouse = Warehouse.objects.filter(pk=data.get("warehouse"), company=company).first() or \
            Warehouse.objects.filter(company=company).first()
        if recipe is None or warehouse is None:
            return Response({"error": "A recipe and a warehouse of your company are required."}, status=400)
        sales_order = None
        if data.get("sales_order"):
            sales_order = SalesOrder.objects.filter(pk=data["sales_order"], customer__company=company).first()
            if sales_order is None:
                return Response({"error": "Customer order not found."}, status=400)
        line = ProductionLine.objects.filter(pk=data.get("line"), company=company).first() if data.get("line") else None
        try:
            order, checks = execution.create_production_order(
                recipe, data.get("quantity"), warehouse,
                sales_order=sales_order,
                plan_ref=data.get("production_plan_ref", ""),
                line=line,
                planned_start=parse_datetime(data["planned_start"]) if data.get("planned_start") else None,
                planned_end=parse_datetime(data["planned_end"]) if data.get("planned_end") else None,
                plan_approved=data.get("plan_approved", True) is not False,
                status="scheduled",
            )
            shortages = []
            if data.get("reserve_materials", True) is not False:
                shortages = execution.reserve_materials(order, request.user)
                if shortages:
                    order.status = "material_pending"
                    order.save(update_fields=["status"])
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Production", "Convert Plan", f"Converted {order.production_plan_ref or 'requirement'} to {order.order_number}")
        return Response({
            "order": ProductionOrderSerializer(order).data,
            "checks": checks,
            "shortages": [{"item": it.name, "quantity": q} for it, q in shortages],
        }, status=status.HTTP_201_CREATED)

    # ---------------------------------------------
    # Lifecycle
    # ---------------------------------------------
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        try:
            order = execution.approve_order(self.get_object(), request.user)
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def start_work(self, request, pk=None):
        order = self.get_object()
        try:
            execution.start_order(order, request.user)
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Production", "Start", f"Started {order.order_number} ({order.recipe.product.name})")
        return Response({"status": "Production started."})

    @action(detail=True, methods=["post"])
    def report_output(self, request, pk=None):
        """Partial production: report finished units; they go to QA, not stock."""
        order = self.get_object()
        try:
            output = execution.report_output(order, request.data.get("quantity"), request.user)
        except ValidationError as e:
            return _bad(e)
        log_activity(request.user, "Production", "Report Output", f"{order.order_number}: {output.quantity} reported, sent to QA")
        order.refresh_from_db()
        return Response({"output": ProductionOutputSerializer(output).data, "order": ProductionOrderSerializer(order).data})

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """Report the whole remaining quantity. Finished goods go to QA; stock
        is only released when QA accepts them."""
        order = self.get_object()
        if order.quantity <= 0:
            return Response({"error": "Production quantity must be greater than zero."}, status=status.HTTP_400_BAD_REQUEST)
        if not order.is_rework and not RecipeIngredient.objects.filter(recipe=order.recipe).exists():
            return Response({"error": "This recipe has no ingredients defined."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            execution.complete_order(order, request.user)
        except ValidationError as e:
            return _bad(e)
        except Exception:
            logging.getLogger(__name__).exception("Production order completion failed (order #%s)", order.id)
            return Response({"error": "Production failed due to internal error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        log_activity(request.user, "Production", "Complete Production Order", f"Completed {order.order_number}: {order.quantity} x '{order.recipe.product.name}' sent to QA")
        return Response({"status": "Production completed successfully."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        try:
            order = execution.close_order(self.get_object(), request.user)
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        try:
            order = execution.cancel_order(self.get_object(), request.user, request.data.get("reason", ""))
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def refresh_materials(self, request, pk=None):
        order = self.get_object()
        execution.refresh_material_status(order)
        return Response({"status": order.status, "material_status": execution.material_status(order)})

    # ---------------------------------------------
    # Detail views
    # ---------------------------------------------
    @action(detail=True, methods=["get"])
    def operations(self, request, pk=None):
        order = self.get_object()
        ops = execution.build_operations(order)
        return Response(ProductionOperationSerializer(ops, many=True).data)

    @action(detail=True, methods=["get"])
    def materials(self, request, pk=None):
        order = self.get_object()
        rows = order.material_requirements.select_related("item")
        return Response({
            "material_status": execution.material_status(order),
            "rows": ProductionMaterialRequirementSerializer(rows, many=True).data,
        })

    @action(detail=True, methods=["get"])
    def outputs(self, request, pk=None):
        return Response(ProductionOutputSerializer(self.get_object().outputs.all(), many=True).data)

    @action(detail=True, methods=["get"])
    def wip(self, request, pk=None):
        from .wip import wip_row
        return Response(wip_row(self.get_object()))

    @action(detail=True, methods=["get"])
    def costing(self, request, pk=None):
        from .costing import order_cost
        return Response(order_cost(self.get_object()))

    @action(detail=True, methods=["get"])
    def capacity_check(self, request, pk=None):
        from .capacity import order_capacity_check
        return Response(order_capacity_check(self.get_object()))

    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        """TB-08: forward-schedule this order's operations on finite capacity."""
        from django.utils.dateparse import parse_datetime
        from .scheduling import schedule_order
        start = parse_datetime(request.data["start"]) if request.data.get("start") else None
        try:
            return Response(schedule_order(self.get_object(), start=start,
                                           commit=request.data.get("dry_run") not in (True, "true", "1")))
        except ValidationError as e:
            return _bad(e)

    @action(detail=True, methods=["post"])
    def record_scrap(self, request, pk=None):
        from inventory.models import Item
        order = self.get_object()
        item = Item.objects.filter(pk=request.data.get("item") or order.recipe.product_id,
                                   company=order.recipe.product.company).first()
        op = order.operations.filter(pk=request.data.get("operation")).first() if request.data.get("operation") else None
        resource = Resource.objects.filter(pk=request.data.get("resource"), company=_company(request)).first() \
            if request.data.get("resource") else None
        if item is None:
            return Response({"error": "Item not found."}, status=400)
        try:
            record = execution.record_scrap(order, item, request.data.get("quantity") or 0, request.user,
                                            operation=op, reason=request.data.get("reason", ""),
                                            resource=resource, source="manual")
        except (ValidationError, ValueError) as e:
            return _bad(e)
        return Response(ScrapRecordSerializer(record).data, status=status.HTTP_201_CREATED)


class ProductionOperationViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Shop-floor operations (TB-03). Quantities change only through actions."""
    company_field = "production_order__recipe__product__company"
    queryset = ProductionOperation.objects.select_related(
        "production_order__recipe__product", "machine", "manpower"
    ).all()
    serializer_class = ProductionOperationSerializer
    permission_classes = [IsProduction | IsAdmin]
    http_method_names = ["get", "patch", "post", "head", "options"]
    filterset_fields = ["production_order", "status", "machine", "manpower"]

    def create(self, request, *args, **kwargs):
        return Response({"error": "Operations come from the product routing."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def perform_update(self, serializer):
        company = _company(self.request)
        for field in ("machine", "manpower"):
            res = serializer.validated_data.get(field)
            if res is not None and res.company_id != company.id:
                raise ValidationError({field: "Pick a resource of your company."})
        execution.check_assignment(
            serializer.instance,
            machine=serializer.validated_data.get("machine"),
            manpower=serializer.validated_data.get("manpower"),
        )
        serializer.save()

    def _resource(self, request, key):
        if not request.data.get(key):
            return None
        return Resource.objects.filter(pk=request.data[key], company=_company(request)).first()

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        try:
            op = execution.start_operation(
                self.get_object(), request.user, quantity=request.data.get("quantity"),
                machine=self._resource(request, "machine"), manpower=self._resource(request, "manpower"),
                operator=request.data.get("operator"),
            )
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOperationSerializer(op).data)

    @action(detail=True, methods=["post"])
    def report(self, request, pk=None):
        d = request.data
        try:
            op = execution.report_operation(
                self.get_object(), request.user,
                good=d.get("good", d.get("completed_quantity", 0)),
                rejected=d.get("rejected", 0), scrap=d.get("scrap", 0), rework=d.get("rework", 0),
                minutes=d.get("minutes"), notes=d.get("notes", ""),
                complete=bool(d.get("complete")), scrap_reason=d.get("scrap_reason", ""),
            )
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOperationSerializer(op).data)

    def _status(self, request, new_status):
        try:
            op = execution.set_operation_status(self.get_object(), new_status, request.user, request.data.get("notes", ""))
        except ValidationError as e:
            return _bad(e)
        return Response(ProductionOperationSerializer(op).data)

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        return self._status(request, "paused")

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        return self._status(request, "in_progress")

    @action(detail=True, methods=["post"])
    def fail(self, request, pk=None):
        return self._status(request, "failed")


class ResourceViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Resource.objects.select_related("line", "equipment", "employee").all()
    serializer_class = ResourceSerializer
    permission_classes = [IsProduction | IsAdmin]
    filterset_fields = ["resource_type", "status", "line", "is_active"]

    @action(detail=True, methods=["get"])
    def capacity(self, request, pk=None):
        from .capacity import _window, resource_capacity
        start, end = _window(days=int(request.query_params.get("days", 7)))
        return Response(resource_capacity(self.get_object(), start, end))


class ResourceUnavailabilityViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "resource__company"
    queryset = ResourceUnavailability.objects.select_related("resource").all()
    serializer_class = ResourceUnavailabilitySerializer
    permission_classes = [IsProduction | IsAdmin]
    filterset_fields = ["resource"]

    def perform_create(self, serializer):
        if serializer.validated_data["resource"].company_id != _company(self.request).id:
            raise ValidationError({"resource": "Pick a resource of your company."})
        serializer.save()


class RoutingStepViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "recipe__product__company"
    queryset = RoutingStep.objects.select_related("recipe__product", "machine", "manpower").all()
    serializer_class = RoutingStepSerializer
    permission_classes = [IsProduction | IsAdmin]
    filterset_fields = ["recipe"]

    def perform_create(self, serializer):
        if serializer.validated_data["recipe"].product.company_id != _company(self.request).id:
            raise ValidationError({"recipe": "Pick a recipe of your company."})
        serializer.save()


class ScrapRecordViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    company_field = "production_order__recipe__product__company"
    queryset = ScrapRecord.objects.select_related("production_order", "item", "operation").all()
    serializer_class = ScrapRecordSerializer
    permission_classes = [IsProduction | IsAdmin | IsQuality]
    filterset_fields = ["production_order", "source"]


class ManufacturingSettingsView(APIView):
    permission_classes = [IsProduction | IsAdmin]

    def get(self, request):
        return Response(ManufacturingSettingsSerializer(ManufacturingSettings.for_company(_company(request))).data)

    def patch(self, request):
        if getattr(request.user, "role", "") != "admin":
            return Response({"error": "Only an admin can change manufacturing settings."}, status=403)
        obj = ManufacturingSettings.for_company(_company(request))
        serializer = ManufacturingSettingsSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class WIPBoardView(APIView):
    """TB-02: where every production order sits on the shop floor."""
    permission_classes = [IsProduction | IsAdmin | IsQuality | IsSales | IsStore]

    def get(self, request):
        from .wip import wip_board
        rows = wip_board(_company(request), include_completed=request.query_params.get("include_completed") == "1")
        so = request.query_params.get("sales_order")
        if so:
            rows = [r for r in rows if str(r["customer_order_id"]) == so]
        return Response(rows)


class CapacityPlanView(APIView):
    """TB-08: machine and manpower capacity for a date window."""
    permission_classes = [IsProduction | IsAdmin]

    def get(self, request):
        from django.utils.dateparse import parse_date
        from .capacity import capacity_plan
        start = parse_date(request.query_params["start"]) if request.query_params.get("start") else None
        end = parse_date(request.query_params["end"]) if request.query_params.get("end") else None
        days = int(request.query_params.get("days", 7))
        return Response(capacity_plan(_company(request), start, end, days))


class ScheduleAllView(APIView):
    """TB-08: reschedule every open production order on finite capacity.
    POST {"dry_run": true} previews without saving."""
    permission_classes = [IsProduction | IsAdmin]

    def post(self, request):
        from .scheduling import schedule_all
        dry_run = request.data.get("dry_run") in (True, "true", "1")
        with transaction.atomic():
            results = schedule_all(_company(request))
            if dry_run:
                transaction.set_rollback(True)
        return Response({"dry_run": dry_run, "orders": results})
