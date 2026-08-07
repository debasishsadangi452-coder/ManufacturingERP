from rest_framework import serializers
from .models import Item, Warehouse, Stock, Batch, InventoryRequest, StockMovement, QuickBooksOnboarding, SalesQuickBooksConfig, ProcurementQuickBooksConfig, BOM, BOMLine, UnitOfMeasure, StockTransfer, CycleCount, CycleCountLine


class StockTransferSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    source_name = serializers.CharField(source="source_warehouse.name", read_only=True)
    dest_name = serializers.CharField(source="dest_warehouse.name", read_only=True)

    class Meta:
        model = StockTransfer
        fields = [
            "id", "item", "item_name", "source_warehouse", "source_name",
            "dest_warehouse", "dest_name", "quantity", "status", "reference",
            "created_at", "completed_at",
        ]
        read_only_fields = ["status", "completed_at"]


class CycleCountLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    variance = serializers.ReadOnlyField()

    class Meta:
        model = CycleCountLine
        fields = ["id", "item", "item_name", "system_quantity", "counted_quantity", "variance"]


class CycleCountSerializer(serializers.ModelSerializer):
    lines = CycleCountLineSerializer(many=True, read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)

    class Meta:
        model = CycleCount
        fields = [
            "id", "warehouse", "warehouse_name", "status", "note",
            "created_at", "posted_at", "lines",
        ]
        read_only_fields = ["status", "posted_at"]


class UnitOfMeasureSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitOfMeasure
        fields = ["id", "code", "name", "dimension", "to_base_factor", "is_base"]

class InventoryRequestSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    item_category = serializers.ReadOnlyField(source='item.category')
    warehouse_name = serializers.ReadOnlyField(source='warehouse.name')

    # Why this material is needed. A request is raised against a production
    # order, which may itself exist to fill a customer order — store needs to
    # see that customer order to judge urgency, not just an internal PO number.
    product_name = serializers.SerializerMethodField()
    production_quantity = serializers.SerializerMethodField()
    sales_order_id = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    origin_label = serializers.SerializerMethodField()

    class Meta:
        model = InventoryRequest
        fields = '__all__'

    def get_product_name(self, obj):
        po = obj.production_order
        return getattr(getattr(getattr(po, 'recipe', None), 'product', None), 'name', None)

    def get_production_quantity(self, obj):
        return getattr(obj.production_order, 'quantity', None)

    def get_sales_order_id(self, obj):
        return getattr(obj.production_order, 'sales_order_id', None)

    def get_customer_name(self, obj):
        so = getattr(obj.production_order, 'sales_order', None)
        return getattr(getattr(so, 'customer', None), 'name', None)

    purchase_order_status = serializers.SerializerMethodField()

    def get_purchase_order_status(self, obj):
        return getattr(obj.purchase_order, 'status', None)

    def get_origin_label(self, obj):
        """Short human phrase naming what this material is for.

        Falls back down the chain: customer order → product being made →
        production order number → a manual request with no production behind it.
        """
        po = obj.production_order
        if po is None:
            return "Manual request"

        product = self.get_product_name(obj)
        so_id = self.get_sales_order_id(obj)
        customer = self.get_customer_name(obj)

        if so_id:
            who = f" for {customer}" if customer else ""
            made = f" ({product})" if product else ""
            return f"Sales Order #{so_id}{who}{made}"
        if product:
            return f"Production of {product} (stock)"
        return f"Production Order #{po.id}"


class ItemSerializer(serializers.ModelSerializer):
    is_finished_good = serializers.ReadOnlyField()
    quickbooks_item_type = serializers.SerializerMethodField()
    procurement_policy = serializers.SerializerMethodField()
    warehouse_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    initial_quantity = serializers.FloatField(write_only=True, required=False, default=0)

    class Meta:
        model = Item
        fields = [
            "id", "name", "category", "erp_classification", "is_finished_good", "unit", "selling_price",
            "quickbooks_id", "quickbooks_sync_token", "quickbooks_last_synced_at",
            "quickbooks_item_type", "procurement_policy", "sku", "purchase_cost",
            "reorder_point", "warehouse_id", "initial_quantity",
            "base_unit", "purchase_unit",
        ]
        read_only_fields = ["quickbooks_id", "quickbooks_sync_token", "quickbooks_last_synced_at"]

    def get_quickbooks_item_type(self, obj):
        return "Inventory" if obj.category == "raw_material" else "Inventory/Sales Item"

    def get_procurement_policy(self, obj):
        if obj.category == "raw_material":
            return "Vendor purchase only"
        return "Finished good for sales/production output"

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
    item_name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = Batch
        fields = [
            "id", "item", "item_name", "batch_number", "quantity",
            "remaining_quantity", "source", "expiry_date", "warehouse",
            "goods_receipt", "production_order", "created_at",
        ]


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


class QuickBooksOnboardingSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuickBooksOnboarding
        fields = ['id', 'company', 'status', 'disclaimer_acknowledged', 'created_at', 'completed_at']
        read_only_fields = ['created_at', 'completed_at']


class SalesQuickBooksConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesQuickBooksConfig
        fields = [
            'id', 'company', 'customers_mapped', 'sale_trigger',
            'default_doc_type', 'price_disclaimer_acknowledged',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['company', 'created_at', 'updated_at']


class ProcurementQuickBooksConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcurementQuickBooksConfig
        fields = [
            'id', 'company', 'vendors_mapped', 'purchase_trigger',
            'cost_source', 'payables_disclaimer_acknowledged',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['company', 'created_at', 'updated_at']


class BOMLineSerializer(serializers.ModelSerializer):
    raw_material_name = serializers.CharField(source='raw_material.name', read_only=True)
    raw_material_unit = serializers.CharField(source='raw_material.unit', read_only=True)

    class Meta:
        model = BOMLine
        fields = ['id', 'raw_material', 'raw_material_name', 'raw_material_unit', 'quantity', 'unit']

    def validate_raw_material(self, value):
        if value.category != "raw_material":
            raise serializers.ValidationError("Only raw materials can be used in BOM.")
        return value


class BOMSerializer(serializers.ModelSerializer):
    lines = BOMLineSerializer(many=True, read_only=True)
    finished_good_name = serializers.CharField(source='finished_good.name', read_only=True)

    class Meta:
        model = BOM
        fields = ['id', 'finished_good', 'finished_good_name', 'lines', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
