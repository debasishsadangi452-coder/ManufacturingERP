from rest_framework import serializers
from .models import Driver, Vehicle, DeliveryRoute, Shipment

class DriverSerializer(serializers.ModelSerializer):
    class Meta:
        model = Driver
        fields = "__all__"

class VehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = "__all__"

class DeliveryRouteSerializer(serializers.ModelSerializer):
    vehicle_name = serializers.ReadOnlyField(source='assigned_vehicle.name')

    class Meta:
        model = DeliveryRoute
        fields = "__all__"

class ShipmentSerializer(serializers.ModelSerializer):
    driver_name = serializers.ReadOnlyField(source='driver.name')
    vehicle_name = serializers.ReadOnlyField(source='vehicle.name')
    route_name = serializers.ReadOnlyField(source='route.name')

    class Meta:
        model = Shipment
        fields = "__all__"
        read_only_fields = ['order_number', 'created_at', 'updated_at']
