from rest_framework import serializers
from .models import Item, Warehouse, Stock, Batch, InventoryRequest, StockMovement, QuickBooksOnboarding, SalesQuickBooksConfig, ProcurementQuickBooksConfig, BOM, BOMLine, UnitOfMeasure, ItemUOMConversion, StockTransfer, CycleCount, CycleCountLine


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


class ItemUOMConversionSerializer(serializers.ModelSerializer):
    from_unit_code = serializers.CharField(source="from_unit.code", read_only=True)
    to_unit_code = serializers.CharField(source="to_unit.code", read_only=True)

    class Meta:
        model = ItemUOMConversion
        fields = ["id", "item", "from_unit", "from_unit_code", "to_unit", "to_unit_code", "factor"]

    def validate(self, attrs):
        request = self.context.get("request")
        company = getattr(getattr(request, "user", None), "company", None)
        item = attrs.get("item", getattr(self.instance, "item", None))
        if company and item and item.company_id not in (None, company.id):
            raise serializers.ValidationError({"item": "Pick an item belonging to your company."})
        for field in ("from_unit", "to_unit"):
            unit = attrs.get(field, getattr(self.instance, field, None))
            if company and unit and unit.company_id not in (None, company.id):
                raise serializers.ValidationError({field: "Pick a shared unit or one belonging to your company."})
        return attrs

class InventoryRequestSerializer(serializers.ModelSerializer):
    item_name = serializers.ReadOnlyField(source='item.name')
    item_category = serializers.ReadOnlyField(source='item.category')
    item_unit = serializers.ReadOnlyField(source='item.unit')
    warehouse_name = serializers.ReadOnlyField(source='warehouse.name')

    # Why this material is needed. Requests may originate from a plan before
    # its production order exists, or from a production order directly.
    product_name = serializers.SerializerMethodField()
    production_quantity = serializers.SerializerMethodField()
    sales_order_id = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    origin_label = serializers.SerializerMethodField()
    production_plan_id = serializers.SerializerMethodField()
    production_plan_number = serializers.SerializerMethodField()

    class Meta:
        model = InventoryRequest
        fields = '__all__'

    def get_product_name(self, obj):
        po = obj.production_order
        product = getattr(getattr(po, 'recipe', None), 'product', None)
        return getattr(product, 'name', None) or getattr(getattr(obj.production_plan, 'item', None), 'name', None)

    def get_production_quantity(self, obj):
        return getattr(obj.production_order, 'quantity', None) or getattr(obj.production_plan, 'planned_quantity', None)

    def get_production_plan_id(self, obj):
        return getattr(obj.production_order, 'production_plan_id', None) or obj.production_plan_id

    def get_production_plan_number(self, obj):
        plan = getattr(obj.production_order, 'production_plan', None) or obj.production_plan
        return getattr(plan, 'plan_number', None)

    def get_sales_order_id(self, obj):
        return getattr(obj.production_order, 'sales_order_id', None) or getattr(obj.production_plan, 'sales_order_id', None)

    def get_customer_name(self, obj):
        so = getattr(obj.production_order, 'sales_order', None)
        if so is None:
            so = getattr(obj.production_plan, 'sales_order', None)
        return getattr(getattr(so, 'customer', None), 'name', None)

    purchase_order_status = serializers.SerializerMethodField()

    def get_purchase_order_status(self, obj):
        return getattr(obj.purchase_order, 'status', None)

    def get_origin_label(self, obj):
        """Short human phrase naming what this material is for.

        Falls back down the chain: customer order → plan/product → production
        order number → a manual request with no production behind it.
        """
        po = obj.production_order
        product = self.get_product_name(obj)
        so_id = self.get_sales_order_id(obj)
        customer = self.get_customer_name(obj)

        if so_id:
            who = f" for {customer}" if customer else ""
            made = f" ({product})" if product else ""
            return f"Sales Order #{so_id}{who}{made}"
        if product:
            return f"Production of {product} (stock)"
        if po is None and obj.production_plan:
            return f"Production Plan {obj.production_plan.plan_number}"
        if po is None:
            return "Manual request"
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
            "reorder_point", "safety_stock", "minimum_order_quantity", "lead_time_days",
            "costing_rule", "default_warehouse", "warehouse_id", "initial_quantity",
            "base_unit", "purchase_unit",
        ]
        read_only_fields = ["quickbooks_id", "quickbooks_sync_token", "quickbooks_last_synced_at"]

    def get_quickbooks_item_type(self, obj):
        return "Inventory" if obj.category in ("raw_material", "intermediate", "semi_finished") else "Inventory/Sales Item"

    def get_procurement_policy(self, obj):
        if obj.category == "raw_material":
            return "Vendor purchase only"
        elif obj.category in ("intermediate", "semi_finished"):
            return "Sub-assembly/Manufactured component"
        return "Finished good for sales/production output"

    def validate(self, attrs):
        request = self.context.get("request")
        company = getattr(getattr(request, "user", None), "company", None)
        warehouse = attrs.get("default_warehouse", getattr(self.instance, "default_warehouse", None))
        if company and warehouse and warehouse.company_id not in (None, company.id):
            raise serializers.ValidationError({"default_warehouse": "Pick a warehouse belonging to your company."})
        for field in ("base_unit", "purchase_unit"):
            unit = attrs.get(field, getattr(self.instance, field, None))
            if company and unit and unit.company_id not in (None, company.id):
                raise serializers.ValidationError({field: "Pick a shared unit or one belonging to your company."})
        initial_warehouse_id = attrs.get("warehouse_id")
        if company and initial_warehouse_id:
            from django.db.models import Q
            if not Warehouse.objects.filter(
                Q(company=company) | Q(company__isnull=True), pk=initial_warehouse_id
            ).exists():
                raise serializers.ValidationError({"warehouse_id": "Pick a warehouse belonging to your company."})
        return attrs

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
        fields = ["id", "name", "location", "warehouse_type", "is_virtual", "is_quarantine", "items"]


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
    accounting_status = serializers.SerializerMethodField()
    journal_entry_id = serializers.SerializerMethodField()
    journal_entry_number = serializers.SerializerMethodField()
    valuation_amount = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            'id', 'item', 'item_name', 'warehouse', 'warehouse_name',
            'movement_type', 'quantity', 'reference', 'created_by_name', 'created_at',
            'accounting_status', 'journal_entry_id', 'journal_entry_number', 'valuation_amount',
        ]

    def _get_journal_entry(self, obj):
        if not hasattr(obj, "_cached_je"):
            from accounting.models import JournalEntry
            obj._cached_je = JournalEntry.objects.filter(
                company_id=obj.item.company_id,
                source_module="inventory",
                source_id=obj.id,
            ).first()
        return obj._cached_je

    def get_accounting_status(self, obj):
        je = self._get_journal_entry(obj)
        if je:
            return je.status
        ref = (obj.reference or "").lower()
        if ref.startswith("transfer to") or ref.startswith("transfer from") or "transfer #" in ref:
            return "not_required"
        return "pending"

    def get_journal_entry_id(self, obj):
        je = self._get_journal_entry(obj)
        return je.id if je else None

    def get_journal_entry_number(self, obj):
        je = self._get_journal_entry(obj)
        return je.entry_number if je else None

    def get_valuation_amount(self, obj):
        cost = getattr(obj.item, "purchase_cost", 0) or 0
        return round(float(abs(obj.quantity)) * float(cost), 2)


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
    raw_material_category = serializers.CharField(source='raw_material.category', read_only=True)

    class Meta:
        model = BOMLine
        fields = [
            'id', 'raw_material', 'raw_material_name', 'raw_material_unit',
            'raw_material_category', 'quantity', 'unit', 'unit_of_measure'
        ]

    def validate_quantity(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value

    def validate_raw_material(self, value):
        if value.category not in ("raw_material", "packaging", "intermediate", "semi_finished"):
            raise serializers.ValidationError("Only materials and semi-finished goods can be used in a BOM.")
        return value


class BOMSerializer(serializers.ModelSerializer):
    lines = BOMLineSerializer(many=True, read_only=True)
    finished_good_name = serializers.CharField(source='finished_good.name', read_only=True)
    finished_good_category = serializers.CharField(source='finished_good.category', read_only=True)

    class Meta:
        model = BOM
        fields = [
            'id', 'finished_good', 'finished_good_name', 'finished_good_category',
            'is_active', 'version', 'status', 'lines', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_finished_good(self, value):
        if value.category not in ("finished_good", "intermediate", "semi_finished"):
            raise serializers.ValidationError("Only finished goods or semi-finished items can have a BOM.")
        return value
