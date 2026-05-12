import json
import time
from django.http import StreamingHttpResponse
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
        user = self.request.user
        if user.role == 'admin':
            return Notification.objects.all().order_by('-created_at')
        return Notification.objects.filter(recipient_role=user.role).order_by('-created_at')

    @action(detail=True, methods=['post'])
    def mark_as_read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.save()
        return Response({'status': 'marked as read'})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        self.get_queryset().update(is_read=True)
        return Response({'status': 'all marked as read'})

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'unread': count})


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdmin]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def live_stream(request):
    """
    SSE endpoint — streams two event types to the connected user:

      event: notification   — a new Notification for this role
      event: change         — a DataChangeEvent for a model this role can see

    Frontend usage:
        const es = new EventSource('/api/core/stream/', { headers: { Authorization: 'Bearer <token>' } });
        es.addEventListener('notification', e => console.log(JSON.parse(e.data)));
        es.addEventListener('change',       e => console.log(JSON.parse(e.data)));
    """
    user = request.user

    def event_stream():
        if user.role == 'admin':
            notif_qs  = Notification.objects.all()
            change_qs = DataChangeEvent.objects.all()
        else:
            notif_qs  = Notification.objects.filter(recipient_role=user.role)
            change_qs = DataChangeEvent.objects.filter(visible_to__contains=[user.role])

        last_notif_id  = notif_qs.order_by('-id').values_list('id', flat=True).first()  or 0
        last_change_id = change_qs.order_by('-id').values_list('id', flat=True).first() or 0

        tick = 0
        while True:
            try:
                # Push new notifications
                for notif in notif_qs.filter(id__gt=last_notif_id).order_by('id'):
                    last_notif_id = notif.id
                    yield (
                        f"event: notification\n"
                        f"data: {json.dumps({'id': notif.id, 'message': notif.message, 'related_type': notif.related_type, 'related_id': notif.related_id, 'is_read': notif.is_read, 'created_at': notif.created_at.isoformat()})}\n\n"
                    )

                # Push new data changes
                for evt in change_qs.filter(id__gt=last_change_id).order_by('id'):
                    last_change_id = evt.id
                    yield (
                        f"event: change\n"
                        f"data: {json.dumps({'model': evt.model_name, 'id': evt.record_id, 'action': evt.action, 'payload': evt.payload, 'at': evt.created_at.isoformat()})}\n\n"
                    )

                # Heartbeat every ~30 s so the connection stays alive
                tick += 1
                if tick % 10 == 0:
                    yield ": heartbeat\n\n"

                time.sleep(3)
            except Exception:
                break

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response
