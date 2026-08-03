from rest_framework import serializers

from .models import Notification, SignalHistory


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ('id', 'title', 'message', 'type', 'is_read', 'created_at')
        read_only_fields = ('id', 'created_at')


def _parse_strategy_name(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason.startswith("[") and "]" in reason:
        token = reason[1:reason.index("]")]
        return {
            "VP": "Volume Profile",
            "ORB": "Opening Range",
            "MOM": "Momentum",
        }.get(token, token)
    lowered = reason.lower()
    if "opening range" in lowered:
        return "Opening Range"
    if "momentum" in lowered:
        return "Momentum"
    if "vah" in lowered or "val" in lowered or "poc" in lowered:
        return "Volume Profile"
    return None


def _parse_market_type(reason: str | None) -> str | None:
    if not reason:
        return None
    first = reason.split(" | ", 1)[0].strip()
    if first.endswith(" day"):
        return first[:-4]
    return first or None


class SignalHistorySerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(read_only=True)
    signal_type = serializers.CharField(read_only=True)
    confidence_score = serializers.SerializerMethodField()
    reasoning_summary = serializers.CharField(source='reason', read_only=True)
    date = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source='generated_at', read_only=True)

    class Meta:
        model = SignalHistory
        fields = (
            'id', 'symbol', 'date',
            'signal_type', 'confidence_score', 'reasoning_summary',
            'status', 'category', 'entry_price', 'target', 'stop_loss',
            'strike_price', 'option_type',
            'created_at',
        )
        read_only_fields = fields

    def get_confidence_score(self, obj):
        rr = float(obj.rr) if obj.rr is not None else 0.0
        return round(min(99.0, max(50.0, 50.0 + (rr * 20.0))), 1)

    def get_date(self, obj):
        return obj.generated_at.date()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["created_at"] = instance.generated_at
        entry = float(instance.entry_price)
        tolerance = max(0.5, abs(entry) * 0.0015)

        # Legacy UI Compatibility Mapping
        data["signal"] = instance.signal_type
        data["strategy"] = instance.signal_type
        data["entry"] = entry
        data["sell_at"] = entry
        data["price"] = entry
        data["target"] = float(instance.target)
        data["stop_loss"] = float(instance.stop_loss)
        data["rr"] = float(instance.rr) if instance.rr is not None else None
        data["signal_confluence"] = instance.status
        data["condition"] = instance.reason
        data["strike"] = f"{float(instance.strike_price)} {instance.option_type}" if instance.strike_price else None
        data["option_expiry"] = instance.option_expiry
        data["signal_time"] = instance.generated_at.isoformat()
        data["active_time"] = instance.active_time.isoformat() if instance.active_time else None
        data["entry_tolerance"] = tolerance
        data["entry_band"] = f"±₹{tolerance:.2f}"
        data["strategy_name"] = _parse_strategy_name(instance.reason)
        
        # Read scanner-computed technicals from the metadata
        if getattr(instance, "metadata", None):
            data["vol_ratio"] = instance.metadata.get("vol_ratio")
            data["vwap"] = instance.metadata.get("vwap")
            data["atr"] = instance.metadata.get("atr")
            data["poc"] = instance.metadata.get("poc")
            data["vah"] = instance.metadata.get("vah")
            data["val"] = instance.metadata.get("val")
            
        data["day_type"] = getattr(instance, "day_type", None)
        data["market_type"] = _parse_market_type(instance.reason) or getattr(instance, "market_type", None) or getattr(instance, "day_type", None)
        data["zone_label"] = getattr(instance, "zone_label", None)
        data["zone_name"] = getattr(instance, "zone_name", None)
        persisted_setup_cmp = float(instance.premium_cmp) if instance.premium_cmp is not None else None
        trigger_gap = round(max(0.5, (persisted_setup_cmp - entry) if persisted_setup_cmp and persisted_setup_cmp > entry else entry * 0.02), 2)
        data["touch_rule"] = getattr(instance, "touch_rule", None)
        data["seller_summary"] = getattr(instance, "seller_summary", None)
        data["trigger_gap"] = trigger_gap
        data["trigger_price"] = entry

        return data
