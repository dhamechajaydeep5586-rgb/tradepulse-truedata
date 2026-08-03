from rest_framework import serializers

from .models import GlobalMarket


class GlobalMarketSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalMarket
        fields = (
            'id', 'date',
            'gift_nifty_ltp',
            'gift_nifty_change',
            'market_bias',
        )
        read_only_fields = fields
