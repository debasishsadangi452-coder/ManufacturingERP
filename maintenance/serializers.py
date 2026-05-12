from rest_framework import serializers
from .models import Equipment, MaintenanceTask

class EquipmentSerializer(serializers.ModelSerializer):
    line_name = serializers.ReadOnlyField(source='line.name')
    
    class Meta:
        model = Equipment
        fields = "__all__"

class MaintenanceTaskSerializer(serializers.ModelSerializer):
    equipment_name = serializers.ReadOnlyField(source='equipment.name')
    line_id = serializers.ReadOnlyField(source='equipment.line.id')
    initiated_by_name = serializers.ReadOnlyField(source='initiated_by.username')
    approved_by_name = serializers.ReadOnlyField(source='approved_by.username')
    technician_fee_display = serializers.SerializerMethodField()

    def get_technician_fee_display(self, obj):
        if obj.technician_fee is not None:
            return f"${float(obj.technician_fee):,.2f}"
        return None

    class Meta:
        model = MaintenanceTask
        fields = "__all__"
