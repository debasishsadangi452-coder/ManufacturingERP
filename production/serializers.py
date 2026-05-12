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
    
    class Meta:
        model = ProductionOrder
        fields = "__all__"

    def get_line_name(self, obj):
        return obj.line.name if obj.line else "Unassigned"
