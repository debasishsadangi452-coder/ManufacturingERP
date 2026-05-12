from django.db import models
from production.models import ProductionOrder


class QualityCheck(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    production_order = models.ForeignKey(
        ProductionOrder,
        on_delete=models.CASCADE
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )

    test_type = models.CharField(max_length=100, blank=True)
    parameter = models.CharField(max_length=100, blank=True)
    result = models.CharField(max_length=100, blank=True)
    target = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)

    inspected_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"QC for Production #{self.production_order.id}"
