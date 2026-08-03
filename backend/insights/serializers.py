from rest_framework import serializers

from .models import Insight


class InsightSerializer(serializers.ModelSerializer):
    class Meta:
        model = Insight
        fields = ('id', 'date', 'ai_summary', 'structured_data_json', 'created_at')
        read_only_fields = fields
