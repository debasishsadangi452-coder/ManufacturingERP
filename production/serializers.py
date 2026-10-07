from rest_framework import serializers
from .models import *

class RecipeSerializer(serializers.ModelSerializer):
    ingredients = serializers.SerializerMethodField()
    product_name = serializers.ReadOnlyField(source='product.name')

    class Meta:
        model = Recipe
        fields = "__all__"

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
    production_plan_number = serializers.ReadOnlyField(source='production_plan.plan_number')
    customer_name = serializers.ReadOnlyField(source='sales_order.customer.name')
    sales_order_number = serializers.SerializerMethodField()
    
    class Meta:
        model = ProductionOrder
        fields = "__all__"

    def get_line_name(self, obj):
        return obj.line.name if obj.line else "Unassigned"

    def get_sales_order_number(self, obj):
        return f"SO-{obj.sales_order.id}" if obj.sales_order else None


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

