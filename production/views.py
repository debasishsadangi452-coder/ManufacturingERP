from django.db import transaction
from datetime import timedelta

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView
from django.utils import timezone

from accounts.permission import IsProduction, IsAdmin
from .models import Recipe, RecipeIngredient, ProductionOrder, ProductionLine
from .serializers import (
    RecipeSerializer,
    RecipeIngredientSerializer,
    ProductionOrderSerializer,
    ProductionLineSerializer,
)
from core.utils import log_activity

class ProductionLineViewSet(viewsets.ModelViewSet):
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

from inventory.services import increase_stock, decrease_stock


# -------------------------------------------------
# 🧾 Recipe ViewSet
# -------------------------------------------------

class RecipeViewSet(viewsets.ModelViewSet):
    queryset = Recipe.objects.select_related('product').all()
    serializer_class = RecipeSerializer
    permission_classes = [IsProduction | IsAdmin]

    def perform_create(self, serializer):
        recipe = serializer.save()
        log_activity(self.request.user, "Production", "Create Recipe", f"Created recipe for '{recipe.product.name}'")

    def perform_destroy(self, instance):
        log_activity(self.request.user, "Production", "Delete Recipe", f"Deleted recipe for '{instance.product.name}'")
        instance.delete()


# -------------------------------------------------
# 🧪 Recipe Ingredient ViewSet
# -------------------------------------------------

class RecipeIngredientViewSet(viewsets.ModelViewSet):
    queryset = RecipeIngredient.objects.all()
    serializer_class = RecipeIngredientSerializer
    permission_classes = [IsProduction | IsAdmin]

    def perform_create(self, serializer):
        ing = serializer.save()
        log_activity(self.request.user, "Production", "Add Recipe Ingredient", f"Added {ing.quantity} x '{ing.item.name}' to recipe for '{ing.recipe.product.name}'")


# -------------------------------------------------
# 🏭 Production Order ViewSet
# -------------------------------------------------

class ProductionOrderViewSet(viewsets.ModelViewSet):
    queryset = ProductionOrder.objects.select_related('recipe__product', 'line', 'warehouse').all()
    serializer_class = ProductionOrderSerializer
    permission_classes = [IsProduction | IsAdmin]

    def perform_create(self, serializer):
        line = serializer.validated_data.get('line')
        start_time = serializer.validated_data.get('start_time')
        end_time = serializer.validated_data.get('end_time')

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
        
        order = serializer.save()
        log_activity(self.request.user, "Production", "Create Production Order", f"Created PO #{order.id} for {order.quantity} x '{order.recipe.product.name}'")

    # ---------------------------------------------
    # 🚀 Start Production
    # ---------------------------------------------
    @action(detail=True, methods=["post"])
    def start_work(self, request, pk=None):
        order = self.get_object()
        if order.status != 'scheduled':
            return Response({"error": "Only scheduled orders can be started."}, status=status.HTTP_400_BAD_REQUEST)
        
        order.status = 'running'
        order.save()
        
        from core.utils import log_activity
        log_activity(request.user, "Production", "Start", f"Started batch #{order.id} ({order.recipe.product.name})")
        
        return Response({"status": "Production started."})

    # ---------------------------------------------
    # 🔥 Complete Production
    # ---------------------------------------------

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):

        order = self.get_object()

        # 1️⃣ Prevent double completion
        if order.status == "completed":
            return Response(
                {"error": "This production order is already completed."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2️⃣ Validate quantity
        if order.quantity <= 0:
            return Response(
                {"error": "Production quantity must be greater than zero."},
                status=status.HTTP_400_BAD_REQUEST
            )

        recipe = order.recipe
        warehouse = order.warehouse

        ingredients = RecipeIngredient.objects.filter(recipe=recipe)

        # 3️⃣ Ensure recipe has ingredients
        if not ingredients.exists():
            return Response(
                {"error": "This recipe has no ingredients defined."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():

                # 🔍 Only deduct ingredients if they were NOT pre-reserved at order creation time.
                # If materials_reserved=True, ingredients were already deducted when the sales order
                # was "marked ready for production", so we must NOT deduct them again.
                if not order.materials_reserved:
                    errors = []
                    for ing in ingredients:
                        required_qty = ing.quantity * order.quantity
                        try:
                            decrease_stock(
                                ing.item,
                                warehouse,
                                required_qty,
                                user=request.user,
                                reference=f"Production #{order.id}"
                            )
                        except (ValidationError, ValueError) as e:
                            errors.append(str(e))

                    if errors:
                        raise ValidationError(errors)

                # 🟢 Add finished goods to inventory
                increase_stock(
                    recipe.product,
                    warehouse,
                    order.quantity,
                    user=request.user,
                    reference=f"Production #{order.id}"
                )

                # Update status
                order.status = "completed"
                order.save()
                log_activity(request.user, "Production", "Complete Production Order", f"Completed PO #{order.id}: {order.quantity} x '{order.recipe.product.name}' added to inventory (materials_reserved={order.materials_reserved})")

        except (ValidationError, ValueError) as e:
            if hasattr(e, 'detail') and isinstance(e.detail, list):
                # Unique messages to avoid redundancy like "Insufficient stock, Insufficient stock"
                unique_msgs = list(dict.fromkeys([str(x) for x in e.detail]))
                msg = " & ".join(unique_msgs)
            elif hasattr(e, 'detail'):
                msg = str(e.detail)
            else:
                msg = str(e)

            return Response(
                {"error": msg},
                status=status.HTTP_400_BAD_REQUEST
            )

        except Exception:
            return Response(
                {"error": "Production failed due to internal error."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(
            {"status": "Production completed successfully."},
            status=status.HTTP_200_OK
        )
