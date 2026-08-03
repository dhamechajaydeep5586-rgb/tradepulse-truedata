from django.db import models
from django.conf import settings

class SignalHistory(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        PENDING = 'PENDING', 'Pending'
        CANCELLED = 'CANCELLED', 'Cancelled'
        HIT_TARGET = 'HIT_TARGET', 'Hit Target'
        HIT_SL = 'HIT_SL', 'Hit SL'
        EXPIRED = 'EXPIRED', 'Expired'

    symbol = models.CharField(max_length=20, db_index=True)
    signal_type = models.CharField(max_length=10)
    entry_price = models.DecimalField(max_digits=12, decimal_places=2)
    stop_loss = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    target = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rr = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )
    category = models.CharField(max_length=20, default='intraday', db_index=True)
    strike_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    option_type = models.CharField(max_length=5, null=True, blank=True) # CE, PE
    premium_cmp = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    option_expiry = models.CharField(max_length=20, null=True, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    active_time = models.DateTimeField(null=True, blank=True)
    whatsapp_signal_sent = models.BooleanField(default=False)
    whatsapp_active_sent = models.BooleanField(default=False)
    whatsapp_exit_sent = models.BooleanField(default=False)
    telegram_signal_sent = models.BooleanField(default=False)
    telegram_active_sent = models.BooleanField(default=False)
    telegram_exit_sent = models.BooleanField(default=False)
    exit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    exit_time = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True, null=True)

    class Meta:
        db_table = 'signal_history'
        ordering = ['-generated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'category', 'status'],
                condition=models.Q(status__in=['PENDING', 'ACTIVE']),
                name='unique_live_signal'
            )
        ]

    def __str__(self):
        return f"{self.symbol} {self.signal_type} @ {self.entry_price} ({self.status})"


class IndexConstituent(models.Model):
    index_name = models.CharField(max_length=50, db_index=True)
    symbol = models.CharField(max_length=30, db_index=True)
    company_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_refreshed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'index_constituents'
        ordering = ['index_name', 'symbol']
        unique_together = [['index_name', 'symbol']]

    def __str__(self):
        return f"{self.index_name}: {self.symbol}"


class SignalChangeLog(models.Model):
    class ChangeType(models.TextChoices):
        SIGNAL_FLIP = 'SIGNAL_FLIP', 'Signal Flip'
        EXPIRY_ROLL = 'EXPIRY_ROLL', 'Expiry Roll'
        ENTRY_UPDATED = 'ENTRY_UPDATED', 'Entry Updated'
        NEW_SIGNAL = 'NEW_SIGNAL', 'New Signal'
        SIGNAL_REMOVED = 'SIGNAL_REMOVED', 'Signal Removed'

    symbol = models.CharField(max_length=30, db_index=True)
    change_type = models.CharField(max_length=20, choices=ChangeType.choices)
    category = models.CharField(max_length=20, default='intraday')
    old_value = models.CharField(max_length=100, blank=True)
    new_value = models.CharField(max_length=100, blank=True)
    reason = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'signal_change_log'
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.symbol} {self.change_type} @ {self.timestamp}"


class Notification(models.Model):
    class TypeChoices(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        ALERT = 'ALERT', 'Alert'
        INFO = 'INFO', 'Info'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    title = models.CharField(max_length=100)
    message = models.TextField()
    type = models.CharField(max_length=20, choices=TypeChoices.choices, default=TypeChoices.INFO)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']


class OptionChain(models.Model):
    symbol = models.CharField(max_length=20, db_index=True)
    spot_price = models.DecimalField(max_digits=12, decimal_places=2)
    pcr = models.DecimalField(max_digits=8, decimal_places=4)
    max_pain = models.DecimalField(max_digits=12, decimal_places=2)
    total_ce_oi = models.BigIntegerField(default=0)
    total_pe_oi = models.BigIntegerField(default=0)
    chain_data_json = models.JSONField(default=list, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'option_chains'
        ordering = ['-timestamp']

    def __str__(self):
        return f"Option Chain: {self.symbol} @ {self.timestamp}"


class Stock(models.Model):
    symbol = models.CharField(max_length=20, db_index=True)
    date = models.DateField(db_index=True)
    open = models.DecimalField(max_digits=12, decimal_places=2)
    high = models.DecimalField(max_digits=12, decimal_places=2)
    low = models.DecimalField(max_digits=12, decimal_places=2)
    close = models.DecimalField(max_digits=12, decimal_places=2)
    volume = models.BigIntegerField()
    delivery_percentage = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    open_interest = models.BigIntegerField(null=True, blank=True)
    previous_open_interest = models.BigIntegerField(null=True, blank=True)
    oi_change = models.BigIntegerField(null=True, blank=True)
    price_change_percent = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    volume_spike_ratio = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'stocks'
        unique_together = [['symbol', 'date']]
        ordering = ['-date', 'symbol']

    def __str__(self):
        return f"{self.symbol} @ {self.date}"


class IntradaySignal(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name='signals')
    date = models.DateField(db_index=True)
    signal_type = models.CharField(max_length=4, choices=[('BUY', 'Buy'), ('SELL', 'Sell'), ('HOLD', 'Hold')])
    confidence_score = models.DecimalField(max_digits=5, decimal_places=2)
    reasoning_summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'intraday_signals'
        unique_together = [['stock', 'date', 'signal_type']]
        ordering = ['-date', '-confidence_score']

    def __str__(self):
        return f"{self.stock.symbol}: {self.signal_type} @ {self.date}"


class MarketHoliday(models.Model):
    market = models.CharField(max_length=20, default='NSE', db_index=True)
    holiday_date = models.DateField(unique=True, db_index=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'market_holidays'
        ordering = ['holiday_date']

    def __str__(self):
        return f"{self.market} holiday: {self.name} on {self.holiday_date}"


class ShortTermSignal(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ACTIVE = 'ACTIVE', 'Active'
        TARGET1 = 'TARGET1', 'Target 1'
        TARGET2 = 'TARGET2', 'Target 2'
        HIT_TARGET = 'HIT_TARGET', 'Hit Target'
        HIT_SL = 'HIT_SL', 'Hit SL'
        TRAILING_EXIT = 'TRAILING_EXIT', 'Trailing Exit'
        TIME_STOP = 'TIME_STOP', 'Time Stop Exit'
        EXPIRED = 'EXPIRED', 'Expired'
        REVIEW_REQUIRED = 'REVIEW_REQUIRED', 'Review Required'
        ARCHIVED = 'ARCHIVED', 'Archived'

    symbol = models.CharField(max_length=20, db_index=True)
    entry_price = models.DecimalField(max_digits=12, decimal_places=2)
    stop_loss = models.DecimalField(max_digits=12, decimal_places=2)
    target = models.DecimalField(max_digits=12, decimal_places=2)
    target2 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Target 2", help_text="Second target price level")
    target3 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Target 3", help_text="Third final target price level")
    current_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )
    vol_ratio = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    setup = models.CharField(max_length=100, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    exited_at = models.DateTimeField(null=True, blank=True)
    exit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    exit_reason = models.CharField(max_length=100, blank=True, verbose_name="Exit Reason", help_text="Detailed reason for trade exit")
    pnl = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    pnl_pct = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    highest_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0.0, verbose_name="Highest Profit", help_text="Peak realized or unrealized profit reached")
    max_drawdown = models.DecimalField(max_digits=12, decimal_places=2, default=0.0, verbose_name="Max Drawdown", help_text="Maximum drawdown percentage or value experienced")
    cooldown_until = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Cooldown Until", help_text="Timestamp until when scanner must ignore this symbol")
    expected_holding_days = models.IntegerField(null=True, blank=True, verbose_name="Expected Holding Days", help_text="ATR-based estimated holding period set at scan time; EOD evaluation force-exits at market price once this elapses without a target/SL hit")
    review_required = models.BooleanField(default=False, verbose_name="Review Required", help_text="Flag indicating trade requires manual assessment")
    ai_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, verbose_name="AI Score", help_text="DEPRECATED — superseded by rank_score. Never written by swing V2.")

    # ── Swing V2 fields ─────────────────────────────────────────────────────────
    # Sizing moves server-side. Previously quantity was computed in React from a
    # user-typed capital box, and a THIRD convention (flat Rs.5,000 risk) was used by
    # the performance report — three surfaces disagreed on the size of one position.
    qty = models.IntegerField(null=True, blank=True, help_text="Risk-parity share count computed at generation")
    rupee_risk = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Rupee risk at entry (qty x per-share risk)")

    # Cross-sectional rank replaces the absolute-threshold ai_score. Named differently
    # on purpose so old and new values are never silently compared.
    rank_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, db_index=True, help_text="Cross-sectional 0-100 rank from shared/ranking.py")
    rank_factors = models.JSONField(default=dict, blank=True, help_text="Per-factor breakdown behind rank_score, for attribution")

    # Regime at generation. Required to measure regime-conditional expectancy later —
    # without it, outcomes cannot be attributed to the conditions that produced them.
    regime_snapshot = models.JSONField(default=dict, blank=True, help_text="RegimeState captured when the signal was generated")

    setup_family = models.CharField(max_length=20, blank=True, db_index=True, help_text="MOMENTUM | PULLBACK")
    cost_pct = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True, help_text="Round-trip cost assumption at generation")
    target_pct = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True, help_text="Target move %, for EV attribution")
    entry_valid_until = models.DateField(null=True, blank=True, help_text="Last session this PENDING entry may activate")

    class Meta:
        db_table = 'short_term_signals'
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['status', '-generated_at'], name='sts_status_gen_idx'),
        ]
        constraints = [
            # Audit remediation plan item 3.1.4: dedup used to rely solely on a
            # scanner-wide cache lock plus an unlocked .exists() check in
            # trade_engine.py, which two overlapping scanner runs could both pass
            # before either inserts. This DB-level partial unique index makes a
            # second live row for the same symbol impossible regardless of any
            # race above it. Mirrors SignalHistory.Meta's `unique_live_signal`.
            # Status list = the non-terminal/live states (PENDING, ACTIVE,
            # TARGET1, TARGET2, REVIEW_REQUIRED); everything else (HIT_TARGET,
            # HIT_SL, TRAILING_EXIT, TIME_STOP, EXPIRED, ARCHIVED — see
            # migration 0032 / Phase 2 #2.7 for the removed dead statuses) is
            # terminal, so multiple rows in those states for the same symbol
            # are fine (e.g. trade history) and intentionally excluded from
            # this constraint.
            models.UniqueConstraint(
                fields=['symbol'],
                condition=models.Q(status__in=[
                    'PENDING', 'ACTIVE', 'TARGET1', 'TARGET2', 'REVIEW_REQUIRED',
                ]),
                name='unique_live_short_term_signal',
            )
        ]

    def __str__(self):
        return f"{self.symbol} {self.status} @ {self.entry_price}"


class StockDailyData(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name='daily_data')
    date = models.DateField(db_index=True)
    open = models.DecimalField(max_digits=12, decimal_places=2)
    high = models.DecimalField(max_digits=12, decimal_places=2)
    low = models.DecimalField(max_digits=12, decimal_places=2)
    close = models.DecimalField(max_digits=12, decimal_places=2)
    volume = models.BigIntegerField()
    delivery_percentage = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    ema20 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ema50 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ema200 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rsi14 = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    adx14 = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    atr14 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    high_52week = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    low_52week = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sector_return = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    nifty_return = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = 'stock_daily_data'
        unique_together = [['stock', 'date']]
        ordering = ['-date']

    def __str__(self):
        return f"{self.stock.symbol} Daily: {self.date}"


class TradeScanner(models.Model):
    class Status(models.TextChoices):
        NEW = 'NEW', 'New'
        PENDING = 'PENDING', 'Pending'
        EXPIRED = 'EXPIRED', 'Expired'
        CANCELLED = 'CANCELLED', 'Cancelled'

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name='scans')
    scan_date = models.DateField(db_index=True)
    ai_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    trend_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    momentum_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    volume_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    sector_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    fundamental_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    risk_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    entry_price = models.DecimalField(max_digits=12, decimal_places=2)
    stop_loss = models.DecimalField(max_digits=12, decimal_places=2)
    target1 = models.DecimalField(max_digits=12, decimal_places=2)
    target2 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    target3 = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    holding_days = models.IntegerField(default=30)
    expiry_days = models.IntegerField(default=30)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)

    class Meta:
        db_table = 'trade_scanner'
        ordering = ['-scan_date']

    def __str__(self):
        return f"Scan: {self.stock.symbol} on {self.scan_date}"


class TelegramLog(models.Model):
    short_term_signal = models.ForeignKey(ShortTermSignal, on_delete=models.CASCADE, related_name='telegram_logs', null=True, blank=True)
    type = models.CharField(max_length=50, verbose_name="Alert Type", help_text="Alert type indicator")
    event_type = models.CharField(max_length=50, blank=True, db_index=True, verbose_name="Event Type", help_text="Dynamic event slug matching strategy lifecycles")
    message = models.TextField()
    status = models.CharField(max_length=20, default='PENDING', db_index=True)
    delivery_status = models.CharField(max_length=20, default='PENDING', db_index=True, verbose_name="Delivery Status", help_text="Real-time delivery progress of Telegram alert")
    retry_count = models.IntegerField(default=0, verbose_name="Retry Count", help_text="Number of delivery retries attempted")
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    chat_id = models.CharField(
        max_length=32, blank=True, default='',
        verbose_name="Target Chat ID",
        help_text="Added 2026-07-28: queue_telegram_message()'s chat_id override used to be "
                   "computed and immediately discarded — process_telegram_queue() hardcoded "
                   "every queued message to the short-term chat regardless. Blank means "
                   "'use the short-term chat', for backward compatibility with rows queued "
                   "before this field existed.",
    )

    class Meta:
        db_table = 'telegram_logs'
        ordering = ['-sent_at']

    def __str__(self):
        symbol = self.short_term_signal.symbol if self.short_term_signal else (self.trade.stock.symbol if self.trade else "UNKNOWN")
        return f"Telegram: {symbol} {self.type} on {self.sent_at}"


class TradeHistory(models.Model):
    trade = models.ForeignKey(ShortTermSignal, on_delete=models.CASCADE, related_name='history', verbose_name="Swing Signal", help_text="ForeignKey pointing to ShortTermSignal")
    old_status = models.CharField(max_length=50, db_index=True, verbose_name="Old Status", help_text="Previous lifecycle state")
    new_status = models.CharField(max_length=50, db_index=True, verbose_name="New Status", help_text="New transitioned lifecycle state")
    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Price", help_text="Execution price during status change")
    reason = models.TextField(blank=True, verbose_name="Reason", help_text="Detailed context or trigger event details")
    triggered_by = models.CharField(max_length=50, default="SYSTEM", verbose_name="Triggered By", help_text="User or automation trigger")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Timestamp", help_text="Audit log creation time")

    class Meta:
        db_table = 'trade_history'
        ordering = ['-timestamp']

    def __str__(self):
        return f"History: {self.trade.symbol} from {self.old_status} to {self.new_status} @ {self.timestamp}"



class CandleBar(models.Model):
    """Locally persisted OHLCV bar.

    Angel One's candle endpoint is rate limited to ~1 call/sec with a 5-minute circuit
    breaker on 403, which caps how large a universe can be scanned and makes bulk
    historical download — the precondition for any walk-forward validation — effectively
    impossible. Persisting bars locally means a scan fetches only the delta since the
    last stored bar instead of re-pulling full history per symbol per cycle.

    See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §9.3.
    """
    symbol = models.CharField(max_length=30, db_index=True)
    exchange = models.CharField(max_length=10, default="NSE")
    interval = models.CharField(max_length=20, db_index=True)
    ts = models.DateTimeField(db_index=True)
    open = models.DecimalField(max_digits=14, decimal_places=2)
    high = models.DecimalField(max_digits=14, decimal_places=2)
    low = models.DecimalField(max_digits=14, decimal_places=2)
    close = models.DecimalField(max_digits=14, decimal_places=2)
    volume = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'candle_bars'
        ordering = ['symbol', 'interval', 'ts']
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'exchange', 'interval', 'ts'],
                name='unique_candle_bar',
            )
        ]
        indexes = [
            models.Index(fields=['symbol', 'interval', 'ts'], name='candle_lookup_idx'),
        ]

    def __str__(self):
        return f"{self.symbol} {self.interval} @ {self.ts}"


class TradeOutcome(models.Model):
    """Append-only ledger of every closed position, across all engines.

    The precondition for expected value, base rates, and Kelly. Before this, closed
    trades were scattered across two models with incompatible status vocabularies, exit
    reasons were only recoverable by string-matching a free-text field, and the regime
    the trade was taken in was not recorded at all — so outcomes could never be
    attributed to the conditions that produced them.

    `kelly_fraction()` gates on 300 observations. This table is what will eventually
    reach that count; until it does, the gate correctly returns 0.0.
    """
    class Engine(models.TextChoices):
        INTRADAY = 'intraday', 'Intraday'
        SWING = 'swing', 'Swing'
        LONG_TERM = 'long_term', 'Long Term'

    engine = models.CharField(max_length=20, choices=Engine.choices, db_index=True)
    symbol = models.CharField(max_length=30, db_index=True)
    setup_family = models.CharField(max_length=40, blank=True, db_index=True)
    direction = models.CharField(max_length=8, default='BUY')

    entry_price = models.DecimalField(max_digits=14, decimal_places=2)
    exit_price = models.DecimalField(max_digits=14, decimal_places=2)
    stop_loss = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    target = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    qty = models.IntegerField(default=0)

    # R-multiple is the unit that makes outcomes comparable across symbols, position
    # sizes and timeframes. Win rate alone is not an edge measure — a 65% hit rate on
    # an asymmetric payoff can still be negative expectancy.
    r_multiple = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True, db_index=True)
    pnl_pct = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    pnl_inr = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    cost_pct = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)

    # Maximum adverse / favourable excursion — how far the trade went against and for
    # us before resolving. Needed to tell "the stop was too tight" from "the signal was
    # wrong", which the same final P&L cannot distinguish.
    mae_pct = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    mfe_pct = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    exit_reason = models.CharField(max_length=40, db_index=True)
    regime_snapshot = models.JSONField(default=dict, blank=True)
    rank_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rank_factors = models.JSONField(default=dict, blank=True)

    holding_days = models.IntegerField(default=0)
    holding_minutes = models.IntegerField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'trade_outcomes'
        ordering = ['-closed_at']
        indexes = [
            models.Index(fields=['engine', 'setup_family', '-closed_at'], name='outcome_engine_setup_idx'),
        ]

    def __str__(self):
        return f"{self.engine}:{self.symbol} {self.exit_reason} R={self.r_multiple}"


class PromoterGroup(models.Model):
    """Maps a symbol to its promoter/business group.

    Exists because SECTOR caps cannot prevent single-group concentration. ADANIPORTS is
    classified Infrastructure and ADANIENT is Diversified, so a 2-per-sector rule admits
    both — which is precisely how the live long-term book reached 100% one promoter
    group across two positions, with correlated financing, news flow and regulatory risk.

    Seeded from a curated list of the major Indian groups; refinable later from NSE/BSE
    shareholding filings.
    """
    symbol = models.CharField(max_length=30, unique=True, db_index=True)
    group_name = models.CharField(max_length=100, db_index=True)
    source = models.CharField(max_length=30, default='curated')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'promoter_groups'
        ordering = ['group_name', 'symbol']

    def __str__(self):
        return f"{self.symbol} -> {self.group_name}"


class CorporateAction(models.Model):
    """Split / bonus / dividend records used to adjust historical price series.

    Unadjusted series manufacture false signals in both directions: a 1:5 split reads as
    an 80% single-bar collapse (a spurious stop-out and a spurious breakdown), and the
    recovery reads as a breakout. Every indicator in the platform consumes daily closes,
    so this is a correctness dependency, not an enhancement.

    NOTE: whether Angel One's getCandleData already returns adjusted series must be
    verified per action type before relying on this table — see the Phase 0 checklist.
    """
    class ActionType(models.TextChoices):
        SPLIT = 'SPLIT', 'Stock Split'
        BONUS = 'BONUS', 'Bonus Issue'
        DIVIDEND = 'DIVIDEND', 'Dividend'
        RIGHTS = 'RIGHTS', 'Rights Issue'
        DEMERGER = 'DEMERGER', 'Demerger'

    symbol = models.CharField(max_length=30, db_index=True)
    ex_date = models.DateField(db_index=True)
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    # For SPLIT/BONUS: the multiplicative price adjustment factor (1:5 split -> 0.2).
    ratio = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    raw_description = models.TextField(blank=True)
    source = models.CharField(max_length=30, default='nse')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'corporate_actions'
        ordering = ['-ex_date', 'symbol']
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'ex_date', 'action_type'],
                name='unique_corporate_action',
            )
        ]

    def __str__(self):
        return f"{self.symbol} {self.action_type} ex-{self.ex_date}"


class EarningsEvent(models.Model):
    """Scheduled results dates, for the swing blackout and the long-term review trigger.

    Populated from NSE's event calendar (already fetched by event_filter_service); this
    table gives it durability and history so a blackout decision can be reconstructed
    after the fact.
    """
    symbol = models.CharField(max_length=30, db_index=True)
    event_date = models.DateField(db_index=True)
    confirmed = models.BooleanField(default=False)
    purpose = models.CharField(max_length=120, blank=True)
    source = models.CharField(max_length=30, default='nse')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'earnings_events'
        ordering = ['-event_date', 'symbol']
        constraints = [
            models.UniqueConstraint(fields=['symbol', 'event_date'], name='unique_earnings_event')
        ]

    def __str__(self):
        return f"{self.symbol} results {self.event_date}"


class DownloadRequest(models.Model):
    """Durable queue row: "fetch candles for symbol X, interval Y, since Z".

    Phase 3 of doc/MARKET_DATA_ENGINE_ARCHITECTURE.md (§2, §9, §12). Same pattern
    already proven in production for `TelegramLog` — a Postgres table with a
    status column, drained by a single poller, instead of pulling in Celery/RQ for
    a workload that is hundreds, not millions, of rows per cycle. Redis is
    deliberately NOT used here (deferred platform-wide for now): this table is
    the queue's backing store, drained via `market_data/download_queue.py`.

    This pass is purely additive — `candle_store.get_candles()` already has its
    own self-sufficient cold-fetch fallback, so a queue that is empty, stalled,
    or never drained changes nothing for any existing engine. See
    doc/MARKET_DATA_ENGINE_ARCHITECTURE.md §12 Phase 3: dual-path, no engine
    depends on this succeeding yet.
    """
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        DONE = 'DONE', 'Done'
        FAILED = 'FAILED', 'Failed'

    symbol = models.CharField(max_length=30, db_index=True)
    exchange = models.CharField(max_length=10, default='NSE')
    interval = models.CharField(max_length=20, db_index=True)
    # Delta-fetch starting point; null means "no known cursor, use a default lookback".
    since_ts = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    attempts = models.IntegerField(default=0)
    last_error = models.TextField(blank=True)
    requested_by = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'download_requests'
        ordering = ['created_at']
        constraints = [
            # Only one non-terminal row per (symbol, exchange, interval) — prevents
            # the same fetch being enqueued twice while it is still pending/running.
            models.UniqueConstraint(
                fields=['symbol', 'exchange', 'interval'],
                condition=models.Q(status__in=['PENDING', 'IN_PROGRESS']),
                name='unique_pending_download_request',
            )
        ]
        indexes = [
            models.Index(fields=['status', 'created_at'], name='download_request_drain_idx'),
        ]

    def __str__(self):
        return f"{self.symbol} {self.exchange} {self.interval} [{self.status}]"


class SignalCandidate(models.Model):
    """Audit trail for the Signal Queue: every candidate an engine finds, not just
    the ones that survive portfolio constraints and become a `SignalHistory` row.

    Phase 5 of doc/MARKET_DATA_ENGINE_ARCHITECTURE.md (§9) writes to this table;
    this migration only creates it correctly ahead of time. Today a rejected
    candidate (sector cap / cluster cap / promoter-group cap / gross exposure /
    cost gate) simply never becomes a `SignalHistory` row and the reason only
    ever existed in logs — this table is what makes that reconstructable after
    the fact.
    """
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        REJECTED = 'REJECTED', 'Rejected'

    engine = models.CharField(max_length=20, db_index=True)
    symbol = models.CharField(max_length=30, db_index=True)
    direction = models.CharField(max_length=8)
    entry = models.DecimalField(max_digits=14, decimal_places=2)
    stop = models.DecimalField(max_digits=14, decimal_places=2)
    target = models.DecimalField(max_digits=14, decimal_places=2)
    rank_score = models.FloatField(null=True, blank=True)
    rank_factors = models.JSONField(default=dict, blank=True)
    regime_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    reject_reason = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'signal_candidates'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.engine}:{self.symbol} {self.direction} [{self.status}]"


class SwingV2ShadowRun(models.Model):
    """One row per day the Swing V2 shadow scan (run_swing_v2_shadow, dry_run=True)
    actually executes. Added 2026-07-29 because that job's own docstring says it
    "persists nothing" by design (no SignalCandidate rows), which left the ~20-session
    shadow-evidence requirement for Phase 5 item A (doc/AUDIT_REMEDIATION_PLAN.md)
    with no durable way to count how many sessions had actually run — Render's
    ephemeral filesystem means a cache-based counter would reset on every redeploy,
    and the log lines themselves aren't queryable. This table is intentionally
    separate from anything a live engine reads, so it can't affect trading behavior
    even if it were ever wrong.
    """
    run_date = models.DateField(unique=True, db_index=True)
    funnel = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'swing_v2_shadow_runs'
        ordering = ['-run_date']

    def __str__(self):
        return f"SwingV2ShadowRun({self.run_date})"
