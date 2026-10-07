from rest_framework import serializers
from .models import *

class RecipeSerializer(serializers.ModelSerializer):
    ingredients = serializers.SerializerMethodField()
    product_name = serializers.ReadOnlyField(source='product.name')
    product_category = serializers.ReadOnlyField(source='product.category')
    default_line_name = serializers.ReadOnlyField(source='default_line.name')

    class Meta:
        model = Recipe
        fields = "__all__"

    def validate(self, attrs):
        line = attrs.get("default_line")
        product = attrs.get("product") or getattr(self.instance, "product", None)
        if line and product and line.company_id != product.company_id:
            raise serializers.ValidationError({"default_line": "Pick a production line of this company."})
        return attrs

    def get_ingredients(self, obj):
        ingredients = RecipeIngredient.objects.filter(recipe=obj)
        return RecipeIngredientSerializer(ingredients, many=True).data

class RecipeIngredientSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    class Meta:
        model = RecipeIngredient
        fields = "__all__"

class ProductionLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionLine
        fields = "__all__"

class ProductionOrderSerializer(serializers.ModelSerializer):
    recipe_name = serializers.ReadOnlyField(source='recipe.product.name')
    line_name = serializers.SerializerMethodField()
    # Intermediate goods (e.g. baked cookies) this batch still lacks; shown as a
    # warning on the work order until enough has been produced.
    intermediate_warnings = serializers.SerializerMethodField()

    def get_intermediate_warnings(self, obj):
        if obj.status not in ("scheduled", "running", "material_pending", "partially_completed") or obj.materials_reserved:
            return []
        from .planning import material_check
        return material_check(obj.recipe, obj.quantity)["intermediate_warnings"]
    production_plan_number = serializers.ReadOnlyField(source='production_plan.plan_number')
    sales_order_number = serializers.SerializerMethodField()
    
    order_number = serializers.ReadOnlyField()
    remaining_quantity = serializers.ReadOnlyField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    qa_status_display = serializers.CharField(source="get_qa_status_display", read_only=True)
    customer_order = serializers.SerializerMethodField()
    customer_name = serializers.ReadOnlyField(source="sales_order.customer.name")
    is_rework = serializers.ReadOnlyField()
    operations_total = serializers.SerializerMethodField()
    operations_completed = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrder
        fields = "__all__"
        read_only_fields = ["produced_quantity", "qa_status", "rework_of", "rework_source_lot", "order_number"]

    def get_customer_order(self, obj):
        return f"SO-{obj.sales_order_id}" if obj.sales_order_id else None

    def get_sales_order_number(self, obj):
        return f"SO-{obj.sales_order_id}" if obj.sales_order_id else None

    def get_operations_total(self, obj):
        return obj.operations.count()

    def get_operations_completed(self, obj):
        return obj.operations.filter(status="completed").count()

    def get_line_name(self, obj):
        return obj.line.name if obj.line else "Unassigned"

# ---------------------------------------------------------------------------
# Track B serializers
# ---------------------------------------------------------------------------

def _same_company(attrs, company, *fields):
    for field in fields:
        obj = attrs.get(field)
        if obj is not None and company is not None and getattr(obj, "company_id", None) not in (None, company.id):
            raise serializers.ValidationError({field: "Pick one belonging to your company."})


class ManufacturingSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ManufacturingSettings
        exclude = ["company"]


class ResourceSerializer(serializers.ModelSerializer):
    line_name = serializers.ReadOnlyField(source="line.name")
    equipment_name = serializers.ReadOnlyField(source="equipment.name")
    employee_name = serializers.ReadOnlyField(source="employee.name")
    resource_type_display = serializers.CharField(source="get_resource_type_display", read_only=True)

    class Meta:
        model = Resource
        exclude = ["company"]
        read_only_fields = ["code", "created_at"]

    def validate(self, attrs):
        company = getattr(self.context.get("request").user, "company", None) if self.context.get("request") else None
        line = attrs.get("line")
        if line is not None and company is not None and line.company_id not in (None, company.id):
            raise serializers.ValidationError({"line": "Pick a production line of your company."})
        employee = attrs.get("employee")
        if employee is not None and company is not None and employee.company_id not in (None, company.id):
            raise serializers.ValidationError({"employee": "Pick an employee of your company."})
        return attrs


class ResourceUnavailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceUnavailability
        fields = "__all__"

    def validate(self, attrs):
        if attrs.get("end") and attrs.get("start") and attrs["end"] <= attrs["start"]:
            raise serializers.ValidationError({"end": "End must be after start."})
        return attrs


class RoutingStepSerializer(serializers.ModelSerializer):
    machine_name = serializers.ReadOnlyField(source="machine.name")
    manpower_name = serializers.ReadOnlyField(source="manpower.name")

    class Meta:
        model = RoutingStep
        fields = "__all__"

    def validate(self, attrs):
        recipe = attrs.get("recipe") or getattr(self.instance, "recipe", None)
        company = recipe.product.company if recipe else None
        for field, kind in (("machine", "machine"), ("manpower", "manpower")):
            res = attrs.get(field)
            if res is not None:
                if res.resource_type != kind:
                    raise serializers.ValidationError({field: f"Pick a {kind} resource."})
                if company is not None and res.company_id != company.id:
                    raise serializers.ValidationError({field: "Pick a resource of this company."})
        return attrs


class ProductionOperationSerializer(serializers.ModelSerializer):
    machine_name = serializers.ReadOnlyField(source="machine.name")
    manpower_name = serializers.ReadOnlyField(source="manpower.name")
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    order_number = serializers.ReadOnlyField(source="production_order.order_number")
    planned_minutes = serializers.ReadOnlyField()
    remaining_minutes = serializers.ReadOnlyField()
    worked_minutes = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOperation
        fields = "__all__"
        read_only_fields = [
            "production_order", "routing_step", "started_quantity", "completed_quantity",
            "rejected_quantity", "rework_quantity", "scrap_quantity", "actual_start", "actual_end",
            "status", "is_default",
        ]

    def get_worked_minutes(self, obj):
        return round(obj.worked_minutes(), 2)


class ProductionMaterialRequirementSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source="item.name")
    unit = serializers.ReadOnlyField(source="item.unit")
    remaining_quantity = serializers.ReadOnlyField()

    class Meta:
        model = ProductionMaterialRequirement
        fields = "__all__"


class ProductionOutputSerializer(serializers.ModelSerializer):
    lot_code = serializers.ReadOnlyField(source="lot.batch_number")
    lot_qa_status = serializers.ReadOnlyField(source="lot.qa_status")

    class Meta:
        model = ProductionOutput
        fields = "__all__"


class ScrapRecordSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source="item.name")
    order_number = serializers.ReadOnlyField(source="production_order.order_number")
    operation_name = serializers.ReadOnlyField(source="operation.name")

    class Meta:
        model = ScrapRecord
        fields = "__all__"
        read_only_fields = ["unit_cost", "cost_impact", "recorded_by", "created_at", "source", "lot"]

class ProductionPlanSerializer(serializers.ModelSerializer):
    sales_order_number = serializers.SerializerMethodField()
    customer_name = serializers.ReadOnlyField(source='customer.name')
    item_name = serializers.ReadOnlyField(source='item.name')
    item_unit = serializers.ReadOnlyField(source='item.unit')
    created_by_name = serializers.ReadOnlyField(source='created_by.username')
    production_orders = serializers.SerializerMethodField()
    recipe_id = serializers.SerializerMethodField()

    class Meta:
        model = ProductionPlan
        fields = [
            "id",
            "plan_number",
            "company",
            "sales_order",
            "sales_order_number",
            "sales_order_item",
            "customer",
            "customer_name",
            "item",
            "item_name",
            "item_unit",
            "order_quantity",
            "planned_quantity",
            "target_date",
            "status",
            "notes",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "production_orders",
            "recipe_id",
        ]
        read_only_fields = ["plan_number", "created_at", "updated_at"]

    def get_sales_order_number(self, obj):
        return f"SO-{obj.sales_order_id}" if obj.sales_order_id else None

    def get_recipe_id(self, obj):
        recipe = Recipe.objects.filter(product=obj.item).first()
        return recipe.id if recipe else None

    def get_production_orders(self, obj):
        return [
            {
                "id": po.id,
                "status": po.status,
                "quantity": po.quantity,
                "warehouse_id": po.warehouse_id,
                "line_id": po.line_id,
                "line_name": po.line.name if po.line else "Unassigned",
                "start_time": po.start_time,
                "end_time": po.end_time,
            }
            for po in obj.production_orders.all()
        ]

    def validate(self, attrs):
        planned_qty = attrs.get('planned_quantity')
        if planned_qty is not None and planned_qty <= 0:
            raise serializers.ValidationError({"planned_quantity": "Planned quantity must be greater than zero."})

        sales_order = attrs.get('sales_order')
        if sales_order:
            if sales_order.status in ['cancelled', 'delivered']:
                raise serializers.ValidationError({"sales_order": f"Cannot create plan for order in '{sales_order.status}' status."})

        return attrs
