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
        if obj.status not in ("scheduled", "running") or obj.materials_reserved:
            return []
        from .planning import material_check
        return material_check(obj.recipe, obj.quantity)["intermediate_warnings"]
    
    class Meta:
        model = ProductionOrder
        fields = "__all__"

    def get_line_name(self, obj):
        return obj.line.name if obj.line else "Unassigned"
