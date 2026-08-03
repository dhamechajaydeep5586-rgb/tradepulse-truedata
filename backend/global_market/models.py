from django.db import models


class GlobalMarket(models.Model):
    class MarketBias(models.TextChoices):
        BULLISH = 'BULLISH', 'Bullish'
        BEARISH = 'BEARISH', 'Bearish'
        NEUTRAL = 'NEUTRAL', 'Neutral'

    date = models.DateField(unique=True, db_index=True)

    # Last traded price (closing price)
    gift_nifty_ltp = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
    )

    # Daily % change
    gift_nifty_change = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
    )
    market_bias = models.CharField(
        max_length=7,
        choices=MarketBias.choices,
        default=MarketBias.NEUTRAL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'global_market'
        ordering = ['-date']

    def __str__(self):
        return f"{self.date} — {self.market_bias}"
