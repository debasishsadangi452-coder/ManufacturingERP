from core.models import AuditLog, Notification

def log_activity(user, module, action, description):
    """
    Utility function to create an audit log entry.
    """
    AuditLog.objects.create(
        user=user,
        module=module,
        action=action,
        description=description
    )

def send_notification(role, message, related_id=None, related_type=""):
    """
    Quality notification helper.
    """
    Notification.objects.create(
        recipient_role=role,
        message=message,
        related_id=related_id,
        related_type=related_type
    )
