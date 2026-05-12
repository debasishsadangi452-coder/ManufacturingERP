from rest_framework import serializers
from .models import Item, Warehouse, Stock, Batch, InventoryRequest, StockMovement

class InventoryRequestSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    item_category = serializers.ReadOnlyField(source='item.category')
    warehouse_name = serializers.ReadOnlyField(source='warehouse.name')
    class Meta:
        model = InventoryRequest
        fields = '__all__'


class ItemSerializer(serializers.ModelSerializer):
    is_finished_good = serializers.ReadOnlyField()
    warehouse_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    initial_quantity = serializers.FloatField(write_only=True, required=False, default=0)

    class Meta:
        model = Item
        fields = ["id", "name", "category", "is_finished_good", "unit", "warehouse_id", "initial_quantity"]

    def create(self, validated_data):
        # Pop these fields so they don't get passed to Item.objects.create()
        validated_data.pop('warehouse_id', None)
        validated_data.pop('initial_quantity', None)
        return super().create(validated_data)


class StockEntrySerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    unit = serializers.ReadOnlyField(source='item.unit')
    class Meta:
        model = Stock
        fields = ["item", "item_name", "quantity", "unit"]

class WarehouseSerializer(serializers.ModelSerializer):
    items = StockEntrySerializer(source='stock_set', many=True, read_only=True)
    class Meta:
        model = Warehouse
        fields = ["id", "name", "location", "items"]


class BatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Batch
        fields = ["id", "item", "batch_number", "quantity"]


class StockSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    warehouse_name = serializers.ReadOnlyField(source='warehouse.name')
    class Meta:
        model = Stock
        fields = "__all__"
        read_only_fields = ("quantity",)

class StockMovementSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    warehouse_name = serializers.ReadOnlyField(source='warehouse.name')
    created_by_name = serializers.ReadOnlyField(source='created_by.username')
    class Meta:
        model = StockMovement
        fields = ['id', 'item', 'item_name', 'warehouse', 'warehouse_name',
                  'movement_type', 'quantity', 'reference', 'created_by_name', 'created_at']
