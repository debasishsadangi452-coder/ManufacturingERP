from django.db import models
from django.conf import settings

class Notification(models.Model):
    """A role-addressed alert, filed under the page the recipient must act on.

    `recipient_role` decides who sees it; `module` decides which page it appears
    on. Admin sees every role's notifications but still segregated by module, so
    the Production page never shows sales chatter.
    """

    MODULE_CHOICES = [
        ("production", "Production"),
        ("inventory", "Inventory"),
        ("sales", "Sales"),
        ("quality", "Quality"),
        ("procurement", "Procurement"),
        ("finance", "Finance"),
        ("maintenance", "Maintenance"),
        ("logistics", "Logistics"),
        ("workforce", "Workforce"),
        ("general", "General"),
    ]

    company = models.ForeignKey(
        "accounts.Company", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    recipient_role = models.CharField(max_length=50) # 'admin', 'production', 'quality', etc.
    module = models.CharField(
        max_length=30, choices=MODULE_CHOICES, default="general", db_index=True,
        help_text="Which page this notification belongs to.",
    )
    message = models.TextField()
    related_id = models.IntegerField(null=True, blank=True)
    related_type = models.CharField(max_length=100, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["company", "recipient_role", "module"],
                name="core_notif_scope_idx",
            ),
        ]

    def __str__(self):
        return f"[{self.module}] {self.recipient_role}: {self.message[:30]}"

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
