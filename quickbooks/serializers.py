from rest_framework import serializers

from .models import QuickBooksConnection, QuickBooksSyncError, QuickBooksSyncRun


class QuickBooksConnectionSerializer(serializers.ModelSerializer):
    connected_by_username = serializers.CharField(source="connected_by.username", read_only=True)

    class Meta:
        model = QuickBooksConnection
        fields = [
            "id",
            "realm_id",
            "environment",
            "company_name",
            "connected_by_username",
            "connected_at",
            "last_synced_at",
            "is_active",
        ]


class QuickBooksSyncRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuickBooksSyncRun
        fields = [
            "id",
            "sync_type",
            "status",
            "records_created",
            "records_updated",
            "records_seen",
            "error_message",
            "started_at",
            "finished_at",
        ]


class QuickBooksSyncErrorSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuickBooksSyncError
        fields = ["id", "entity_type", "quickbooks_id", "message", "payload", "created_at"]
