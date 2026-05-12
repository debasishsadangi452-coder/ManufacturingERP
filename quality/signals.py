from django.db.models.signals import post_save
from django.dispatch import receiver
from production.models import ProductionOrder
from .models import QualityCheck

@receiver(post_save, sender=ProductionOrder)
def create_quality_check(sender, instance, created, **kwargs):
    # Only create quality check when the production order status is updated to 'completed'
    # And if a quality check doesn't already exist for it
    if instance.status == "completed":
        QualityCheck.objects.get_or_create(production_order=instance)
