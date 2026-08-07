from django.db.utils import NotSupportedError
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import Notification, AuditLog, DataChangeEvent
from .serializers import NotificationSerializer, AuditLogSerializer
from accounts.permission import IsAdmin


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Scope by tenant, then role, then page.

        Admin is exempt from the role filter — they see every role's traffic —
        but never from the module filter, so the Production page shows only
        production notifications regardless of who is looking at it.
        """
        user = self.request.user
        qs = Notification.objects.filter(company=user.company)
        if user.role != 'admin':
            qs = qs.filter(recipient_role=user.role)

        module = self.request.query_params.get('module')
        if module and module != 'all':
            qs = qs.filter(module__in=[m.strip() for m in module.split(',') if m.strip()])

        if self.request.query_params.get('unread') == 'true':
            qs = qs.filter(is_read=False)

        return qs.order_by('-created_at')

    @action(detail=True, methods=['post'])
    def mark_as_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.save()
        return Response({'status': 'marked as read'})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark read. Honours ?module=, so clearing one page leaves others alone."""
        updated = self.get_queryset().update(is_read=True)
        return Response({'status': 'all marked as read', 'updated': updated})

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Unread total, plus a per-module breakdown for page badges."""
        from django.db.models import Count

        user = request.user
        base = Notification.objects.filter(company=user.company, is_read=False)
        if user.role != 'admin':
            base = base.filter(recipient_role=user.role)

        by_module = {
            row['module']: row['total']
            for row in base.values('module').annotate(total=Count('id'))
        }
        return Response({
            'unread': sum(by_module.values()),
            'by_module': by_module,
        })


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdmin]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def poll_changes(request):
    """
    Lightweight poll endpoint — replaces the blocking SSE stream.
    Frontend calls this every 10 s with ?since=<last_change_id>&nonce=<last_notif_id>.
    Returns any new DataChangeEvents and Notifications since those IDs.
    """
    user = request.user
    since_change = int(request.query_params.get('since', 0))
    since_notif  = int(request.query_params.get('nonce', 0))

    if user.role == 'admin':
        change_qs = DataChangeEvent.objects.filter(id__gt=since_change).order_by('id')[:50]
        notif_qs  = Notification.objects.filter(id__gt=since_notif, company=user.company).order_by('id')[:20]
    else:
        try:
            change_qs = list(DataChangeEvent.objects.filter(
                id__gt=since_change, visible_to__contains=[user.role]
            ).order_by('id')[:50])
        except NotSupportedError:
            # sqlite (local dev) can't do JSONField contains — filter in Python.
            change_qs = [
                e for e in DataChangeEvent.objects.filter(id__gt=since_change).order_by('id')[:200]
                if user.role in (e.visible_to or [])
            ][:50]
        notif_qs  = Notification.objects.filter(
            id__gt=since_notif, recipient_role=user.role, company=user.company
        ).order_by('id')[:20]

    changes = [
        {'model': e.model_name, 'id': e.record_id, 'action': e.action, 'change_id': e.id}
        for e in change_qs
    ]
    notifications = [
        {'id': n.id, 'message': n.message, 'is_read': n.is_read,
         'created_at': n.created_at.isoformat()}
        for n in notif_qs
    ]
    return Response({'changes': changes, 'notifications': notifications})
