from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Equipment, MaintenanceTask
from .serializers import EquipmentSerializer, MaintenanceTaskSerializer
from accounts.permission import IsAdmin, IsMaintenanceAllowed
from core.models import Notification
from core.utils import log_activity

class EquipmentViewSet(viewsets.ModelViewSet):
    queryset = Equipment.objects.all()
    serializer_class = EquipmentSerializer
    
    def get_permissions(self):
        if self.action in ["create", "update", "partial_update", "destroy"]:
            return [IsAdmin()]
        return [IsMaintenanceAllowed()]

class MaintenanceTaskViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceTask.objects.all()
    serializer_class = MaintenanceTaskSerializer
    
    def get_permissions(self):
        if self.action == "destroy":
            return [IsAdmin()]
        return [IsMaintenanceAllowed()]

    def perform_create(self, serializer):
        user = self.request.user
        status_val = "requested"
        
        # If a scheduled date is provided, it can be scheduled immediately
        # unless explicitly requested by production as a "fix this" request
        if serializer.validated_data.get('scheduled_date') and user.role != "production":
            status_val = "scheduled"
            
        task = serializer.save(status=status_val, initiated_by=user)
        
        if status_val == "requested":
            Notification.objects.create(
                recipient_role="quality",
                message=f"Maintenance requested for {task.equipment.name} by {user.username}.",
                related_id=task.id,
                related_type="MaintenanceTask"
            )
            log_activity(user, "Maintenance", "Request", f"Requested maintenance for {task.equipment.name}")
        else:
            log_activity(user, "Maintenance", "Schedule", f"Scheduled maintenance for {task.equipment.name}")

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        if not (request.user.role == 'quality' or request.user.role == 'admin'):
            return Response({'error': 'Only Quality or Admin users can reject requests'}, status=status.HTTP_403_FORBIDDEN)
        
        task = self.get_object()
        if task.status != 'requested':
            return Response({'error': 'Only requested tasks can be rejected'}, status=status.HTTP_400_BAD_REQUEST)
        
        reason = request.data.get('rejection_reason', 'No reason provided')
        task.status = 'rejected'
        task.rejection_reason = reason
        task.save()

        Notification.objects.create(
            recipient_role="production",
            message=f"Maintenance request for {task.equipment.name} was REJECTED: {reason}",
            related_id=task.id,
            related_type="MaintenanceTask"
        )
        log_activity(request.user, "Maintenance", "Reject", f"Rejected maintenance request for {task.equipment.name}")
        
        return Response({'status': 'Task rejected'})

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        # Allow production, quality, or admin to start work
        if request.user.role not in ['production', 'quality', 'admin']:
            return Response({'error': 'Only Production, Quality technicians, or Admins can start work'}, status=status.HTTP_403_FORBIDDEN)

        task = self.get_object()
        if task.status != 'scheduled':
            return Response({'error': 'Task is not in scheduled state'}, status=status.HTTP_400_BAD_REQUEST)
        
        from django.utils import timezone
        task.status = 'in-progress'
        task.started_at = timezone.now()
        task.save()
        
        equipment = task.equipment
        equipment.status = 'maintenance'
        equipment.save()

        line = equipment.line
        line.status = 'maintenance'
        line.save()
        
        log_activity(request.user, "Maintenance", "Start", f"Started maintenance for {equipment.name}")
        return Response({'status': 'Task started and line set to maintenance'})

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        # Allow production, quality, or admin to complete work
        if request.user.role not in ['production', 'quality', 'admin']:
            return Response({'error': 'Only Production, Quality technicians, or Admins can complete work'}, status=status.HTTP_403_FORBIDDEN)

        task = self.get_object()
        if task.status != 'in-progress':
            return Response({'error': 'Task is not in-progress'}, status=status.HTTP_400_BAD_REQUEST)
        
        from django.utils import timezone
        task.status = 'completed'
        task.completed_at = timezone.now()
        task.save()
        
        equipment = task.equipment
        equipment.status = 'idle'
        equipment.save()
        
        # Calculate duration
        duration = task.completed_at - task.started_at
        hours, remainder = divmod(duration.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        duration_str = f"{int(hours)}h {int(minutes)}m"

        Notification.objects.create(
            recipient_role="quality",
            message=f"Maintenance for {equipment.name} completed in {duration_str}. Approval required.",
            related_id=task.id,
            related_type="MaintenanceTask"
        )
        log_activity(request.user, "Maintenance", "Complete", f"Completed maintenance work for {equipment.name} in {duration_str}")
        return Response({'status': f'Task completed in {duration_str}, awaiting quality approval'})

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        if not (request.user.role == 'quality' or request.user.role == 'admin'):
            return Response({'error': 'Only Quality or Admin users can approve maintenance'}, status=status.HTTP_403_FORBIDDEN)

        task = self.get_object()
        if task.status == 'requested':
            # Support updating scheduling + technician details during approval
            task.scheduled_date = request.data.get('scheduled_date', task.scheduled_date)
            task.assignee = request.data.get('assignee', task.assignee)
            task.estimated_duration = request.data.get('estimated_duration', task.estimated_duration)
            task.technician_name = request.data.get('technician_name', task.technician_name)
            fee_raw = request.data.get('technician_fee', None)
            if fee_raw not in (None, '', 'null'):
                try:
                    task.technician_fee = float(fee_raw)
                except (ValueError, TypeError):
                    pass

            if not task.scheduled_date:
                return Response({'error': 'Please provide a scheduled date and time'}, status=status.HTTP_400_BAD_REQUEST)

            task.status = 'scheduled'
            task.save()
            fee_info = f" | Technician: {task.technician_name} @ ${task.technician_fee}" if task.technician_name else ""
            log_activity(request.user, "Maintenance", "Approve Request", f"Approved and scheduled maintenance request for {task.equipment.name} at {task.scheduled_date}{fee_info}")
            return Response({'status': 'Request approved and scheduled'})

        if task.status != 'completed':
            return Response({'error': 'Task must be completed before final approval'}, status=status.HTTP_400_BAD_REQUEST)
        
        task.is_approved = True
        task.approved_by = request.user
        task.save()

        equipment = task.equipment
        equipment.status = 'running'
        equipment.health = 100
        equipment.save()

        line = equipment.line
        has_other_maintenance = MaintenanceTask.objects.filter(
            equipment__line=line, 
            status__in=['scheduled', 'in-progress']
        ).exists()
        
        if not has_other_maintenance:
            line.status = 'running'
            line.save()
        
        # Notify production about duration
        duration = task.completed_at - task.started_at
        hours, remainder = divmod(duration.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        duration_str = f"{int(hours)}h {int(minutes)}m"

        Notification.objects.create(
            recipient_role="production",
            message=f"Maintenance for {equipment.name} APPROVED. Total time: {duration_str}",
            related_id=task.id,
            related_type="MaintenanceTask"
        )

        log_activity(request.user, "Maintenance", "Final Approval", f"Approved maintenance completion for {equipment.name}")
        return Response({'status': 'Maintenance approved and line restored'})
