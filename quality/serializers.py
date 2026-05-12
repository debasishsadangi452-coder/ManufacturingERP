from rest_framework import serializers
from .models import QualityCheck


class QualityCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = QualityCheck
        fields = "__all__"
