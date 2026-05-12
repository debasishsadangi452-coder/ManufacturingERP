from django.contrib import admin
from .models import Recipe, RecipeIngredient, ProductionOrder


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    extra = 1


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ("product",)
    search_fields = ("product__name",)
    inlines = [RecipeIngredientInline]


@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "recipe",
        "quantity",
        "warehouse",
        "status",
        "created_at",
    )
    list_filter = ("status", "warehouse", "created_at")
    search_fields = ("recipe__product__name",)
    readonly_fields = ("created_at",)