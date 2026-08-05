from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Driver, Vehicle, DeliveryRoute, Shipment
from .serializers import DriverSerializer, VehicleSerializer, DeliveryRouteSerializer, ShipmentSerializer
from accounts.permission import IsAdmin, IsSales, IsStore, IsQuality
from core.utils import log_activity
from core.tenancy import CompanyScopedMixin

class DriverViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Driver.objects.all()
    serializer_class = DriverSerializer
    permission_classes = [IsSales | IsStore | IsAdmin | IsQuality]

    def perform_create(self, serializer):
        driver = serializer.save(company=self.request.user.company)
        log_activity(self.request.user, "Logistics", "Add Driver", f"Added driver '{driver.name}' (License: {driver.license_number})")

class VehicleViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    permission_classes = [IsSales | IsStore | IsAdmin | IsQuality]

    def perform_create(self, serializer):
        vehicle = serializer.save(company=self.request.user.company)
        log_activity(self.request.user, "Logistics", "Add Vehicle", f"Added vehicle '{vehicle.name}' ({vehicle.vehicle_type})")

    @action(detail=True, methods=['post'], url_path='dispatch')
    def dispatch_vehicle(self, request, pk=None):
        vehicle = self.get_object()
        if vehicle.status != 'available':
            return Response({'error': 'Vehicle is not available'}, status=status.HTTP_400_BAD_REQUEST)

        vehicle.status = 'in-use'
        vehicle.save()
        log_activity(request.user, "Logistics", "Dispatch Vehicle", f"Dispatched vehicle '{vehicle.name}'")
        return Response({'status': 'Vehicle dispatched'})

class DeliveryRouteViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = DeliveryRoute.objects.all()
    serializer_class = DeliveryRouteSerializer
    permission_classes = [IsSales | IsStore | IsAdmin | IsQuality]

    def perform_create(self, serializer):
        route = serializer.save(company=self.request.user.company)
        log_activity(self.request.user, "Logistics", "Create Route", f"Created delivery route '{route.name}' with {route.stops} stops")

    @action(detail=True, methods=['post'], url_path='start')
    def start_route(self, request, pk=None):
        route = self.get_object()
        if route.status != 'planned':
            return Response({'error': 'Route is not in planned state'}, status=status.HTTP_400_BAD_REQUEST)

        route.status = 'active'
        route.save()
        log_activity(request.user, "Logistics", "Start Route", f"Started delivery route '{route.name}'")
        return Response({'status': 'Route started'})

class ShipmentViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    company_field = "company"
    queryset = Shipment.objects.all().order_by('-created_at')
    serializer_class = ShipmentSerializer
    permission_classes = [IsSales | IsStore | IsAdmin | IsQuality]

    def perform_create(self, serializer):
        shipment = serializer.save(company=self.request.user.company)
        log_activity(self.request.user, "Logistics", "Create Shipment", f"Created shipment '{shipment.order_number}' to '{shipment.destination}'")

    @action(detail=True, methods=['post'], url_path='update_status')
    def update_status(self, request, pk=None):
        shipment = self.get_object()
        new_status = request.data.get('status')
        valid_statuses = [s[0] for s in Shipment.STATUS_CHOICES]
        if new_status not in valid_statuses:
            return Response({'error': f'Invalid status. Must be one of: {valid_statuses}'}, status=status.HTTP_400_BAD_REQUEST)

        shipment.status = new_status
        if new_status == 'in-transit' and shipment.progress == 0:
            shipment.progress = 10
        elif new_status == 'delivered':
            shipment.progress = 100
        shipment.save()
        log_activity(request.user, "Logistics", "Update Shipment", f"Shipment '{shipment.order_number}' status → {new_status}")
        return Response(ShipmentSerializer(shipment).data)
