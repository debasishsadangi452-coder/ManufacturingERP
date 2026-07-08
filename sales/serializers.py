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
        company = order.customer.company

        from decimal import Decimal
        from rest_framework import serializers as drf_serializers
        from inventory.models import Item
        total = Decimal("0")
        for item_data in items_data:
            item_id = item_data.get('item')
            quantity = item_data.get('quantity', 0)
            if item_id and float(quantity) > 0:
                # An order may only contain items from the customer's own company
                if not Item.objects.filter(id=int(item_id), company=company).exists():
                    order.delete()
                    raise drf_serializers.ValidationError(
                        {"items": f"Item {item_id} does not belong to {company}."}
                    )
                so_item = SalesOrderItem.objects.create(
                    sales_order=order,
                    item_id=int(item_id),
                    quantity=float(quantity)
                )
                total += (so_item.item.selling_price or Decimal("0")) * Decimal(str(so_item.quantity))
        # Price the order from item selling prices unless a total was supplied
        if not order.total_amount and total:
            order.total_amount = total
            order.save(update_fields=["total_amount"])
        return order

class ShipmentSerializer(serializers.ModelSerializer):
    sales_order_id = serializers.IntegerField(source="sales_order.id", read_only=True)
    class Meta:
        model = Shipment
        fields = "__all__"
