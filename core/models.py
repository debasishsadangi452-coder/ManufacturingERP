from django.db import models
from django.conf import settings

class Notification(models.Model):
    recipient_role = models.CharField(max_length=50) # 'admin', 'production', 'quality', etc.
    message = models.TextField()
    related_id = models.IntegerField(null=True, blank=True)
    related_type = models.CharField(max_length=100, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.recipient_role}: {self.message[:30]}"

class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    module = models.CharField(max_length=100)
    action = models.CharField(max_length=255)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} - {self.module} - {self.action}"


class DataChangeEvent(models.Model):
    """Records every create/update to shared models so the SSE stream can relay them."""
    model_name = models.CharField(max_length=100)
    record_id = models.IntegerField()
    action = models.CharField(max_length=20)      # 'created' | 'updated' | 'deleted'
    payload = models.JSONField(default=dict)       # key fields of the changed record
    visible_to = models.JSONField(default=list)    # list of role strings that can see this

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.action} {self.model_name} #{self.record_id}"
