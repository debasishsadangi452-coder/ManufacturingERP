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

def send_notification(role, message, related_id=None, related_type="", company=None,
                      module="general"):
    """
    Create a role-addressed notification filed under `module`.

    `module` decides which page shows it — see Notification.MODULE_CHOICES.
    `company` scopes it to a tenant; without it the notification is invisible to
    everyone, because the viewset filters on the caller's company.
    """
    Notification.objects.create(
        recipient_role=role,
        message=message,
        related_id=related_id,
        related_type=related_type,
        company=company,
        module=module,
    )
