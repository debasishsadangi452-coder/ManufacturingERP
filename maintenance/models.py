from django.db import models
from production.models import ProductionLine
from django.conf import settings
from decimal import Decimal

class Equipment(models.Model):
    STATUS_CHOICES = [
        ("running", "Running"),
        ("idle", "Idle"),
        ("maintenance", "Maintenance"),
        ("breakdown", "Breakdown"),
    ]
    
    name = models.CharField(max_length=200)
    line = models.ForeignKey(ProductionLine, on_delete=models.CASCADE, related_name="equipment")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running")
    health = models.IntegerField(default=100)
    last_maintenance = models.DateField(null=True, blank=True)
    next_maintenance = models.DateField(null=True, blank=True)
    uptime = models.FloatField(default=100.0)

    def __str__(self):
        return self.name

class MaintenanceTask(models.Model):
    TYPE_CHOICES = [
        ("preventive", "Preventive"),
        ("corrective", "Corrective"),
        ("predictive", "Predictive"),
    ]
    
    STATUS_CHOICES = [
        ("requested", "Requested"),
        ("scheduled", "Scheduled"),
        ("in-progress", "In Progress"),
        ("completed", "Completed"),
        ("rejected", "Rejected"),
        ("overdue", "Overdue"),
    ]
    
    PRIORITY_CHOICES = [
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]
    
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE)
    task_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="requested")
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES)
    assignee = models.CharField(max_length=100, null=True, blank=True)
    scheduled_date = models.DateTimeField(null=True, blank=True)
    estimated_duration = models.CharField(max_length=50, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    
    rejection_reason = models.TextField(null=True, blank=True)
    technician_name = models.CharField(max_length=200, null=True, blank=True, help_text="Full name of assigned technician")
    technician_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Agreed fee for the technician")
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="General maintenance cost or requested fee")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="initiated_maintenance_tasks"
    )
    
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    is_approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_maintenance_tasks"
    )

    def __str__(self):
        return f"{self.task_type} for {self.equipment.name}"
