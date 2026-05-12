from rest_framework import serializers
from .models import *
from inventory.models import Item

class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = "__all__"

class SalesOrderItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    
    class Meta:
        model = SalesOrderItem
        fields = ["id", "item", "item_name", "quantity", "shipped_quantity"]

class SalesOrderSerializer(serializers.ModelSerializer):
    # Read-only nested items for display
    items = SalesOrderItemSerializer(source="salesorderitem_set", many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)

    class Meta:
        model = SalesOrder
        fields = ["id", "customer", "customer_name", "created_at", "total_amount", "status", "items"]

    def create(self, validated_data):
        # Items come from raw request data (not serializer), create order first
        request = self.context.get('request')
        items_data = request.data.get('items', []) if request else []
        
        order = SalesOrder.objects.create(**validated_data)
        
        for item_data in items_data:
            item_id = item_data.get('item')
            quantity = item_data.get('quantity', 0)
            if item_id and float(quantity) > 0:
                SalesOrderItem.objects.create(
                    sales_order=order,
                    item_id=int(item_id),
                    quantity=float(quantity)
                )
        return order

class ShipmentSerializer(serializers.ModelSerializer):
    sales_order_id = serializers.IntegerField(source="sales_order.id", read_only=True)
    class Meta:
        model = Shipment
        fields = "__all__"
