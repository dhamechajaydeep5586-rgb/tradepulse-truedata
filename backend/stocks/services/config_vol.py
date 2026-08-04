# config_vol.py
# Centralized Systematic Short Volatility parameters
# Dual-mode: Monthly (conservative) + Intraday (theta-velocity optimized)

# ─────────────────────────────────────────────────────────────
# 1. COMBINED PREMIUM STOP LOSS
# ─────────────────────────────────────────────────────────────
COMBINED_SL_MULTIPLIER = 1.30        # Monthly mode: exit if combined premium expands 30%
INTRADAY_COMBINED_SL_MULT = 1.20     # Intraday mode: tighter 20% expansion SL (was 1.25 — too slow to exit)

# ─────────────────────────────────────────────────────────────
# 2. PROFIT CAPTURE BOOKING
# ─────────────────────────────────────────────────────────────
PROFIT_CAPTURE_PCT = 0.80            # Monthly: book when 80% of premium decayed
INTRADAY_PROFIT_CAPTURE_PCT = 0.25   # Intraday: fast capture at 25% decay (theta already extracted)
INTRADAY_PROFIT_CAPTURE_EARLY = 0.20 # Very fast exit: 20% decay if DTE ≤ 3 (gamma risk)

# ─────────────────────────────────────────────────────────────
# 3. DELTA DANGER PARAMETERS
# ─────────────────────────────────────────────────────────────
SHORT_DELTA_DANGER = 0.55            # Critical delta — auto-exit strangle
AUTO_EXIT_ON_DELTA_BREACH = True

# Audit fix (C4): NSE stock options (unlike index options) are American-style and
# physically settled — a short leg that drifts/gaps ITM can be assigned before
# expiry, obligating physical delivery rather than a cash settlement. This threshold
# is deliberately BELOW SHORT_DELTA_DANGER: it's an early informational warning
# specific to physically-settled stock legs, not an auto-exit trigger — the strangle
# engine has no code path that distinguishes stock-option risk from index-option risk
# otherwise.
ASSIGNMENT_RISK_DELTA = 0.35
# Index underlyings settle in cash (no physical-delivery/assignment risk at all) —
# same set truedata_service.get_option_chain() classifies as OPTIDX. Everything else
# this engine trades is a single-stock underlying, i.e. OPTSTK.
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}

# ─────────────────────────────────────────────────────────────
# 4. EXPIRY / GAMMA MANAGEMENT
# ─────────────────────────────────────────────────────────────
FORCE_EXIT_DTE = 1                   # Days to expiry to force-exit and block new entries

# ─────────────────────────────────────────────────────────────
# 5. EXECUTION CONTROLS
# ─────────────────────────────────────────────────────────────
MAX_SPREAD_PCT = 0.05                # 5% max bid-ask spread relative to mid-price

# ─────────────────────────────────────────────────────────────
# 6. VOLATILITY REGIME BOUNDARIES
# ─────────────────────────────────────────────────────────────
IV_LOW_THRESHOLD = 0.15              # Low IV regime: below 15%
IV_HIGH_THRESHOLD = 0.25             # High IV regime: above 25%
IV_PANIC_THRESHOLD = 0.40            # Panic IV regime: above 40%

# ─────────────────────────────────────────────────────────────
# 7. MINIMUM COMBINED PREMIUM TARGETS (Monthly / Conservative)
# ─────────────────────────────────────────────────────────────
MIN_COMBINED_PREMIUM_LARGECAP = 12.0
MIN_COMBINED_PREMIUM_MIDCAP = 18.0
MIN_COMBINED_PREMIUM_HIGHIV = 25.0

# ─────────────────────────────────────────────────────────────
# 8. MINIMUM COMBINED PREMIUM TARGETS (Intraday)
# Lower floor acceptable intraday because decay is measured in hours
# ─────────────────────────────────────────────────────────────
INTRADAY_MIN_PREMIUM_MORNING = 8.0    # 09:20–11:00 session floor (raised from ₹5 to ensure viable premium)
INTRADAY_MIN_PREMIUM_MIDDAY = 10.0   # 11:00–13:00 session floor (raised from ₹6)
INTRADAY_MIN_PREMIUM_AFTERNOON = 12.0 # 13:00–15:15 session floor (raised from ₹8)

# ─────────────────────────────────────────────────────────────
# 9. ADAPTIVE DELTA RANGE (Intraday)
# Time-based aggression: start safer in morning, compress strikes as day progresses
# ─────────────────────────────────────────────────────────────
INTRADAY_DELTA_MORNING = 0.25         # 09:20–11:00 (safer opening strikes, was 0.35 — too aggressive)
INTRADAY_DELTA_MIDDAY = 0.28          # 11:00–13:00 (moderate compression, was 0.38)
INTRADAY_DELTA_AFTERNOON = 0.32       # 13:00–15:15 (maximum decay capture, was 0.42)
INTRADAY_MAX_DELTA = 0.40             # Hard cap: never sell above this delta intraday (was 0.48)

# ─────────────────────────────────────────────────────────────
# 10. ADAPTIVE DELTA RANGE (Monthly / Conservative)
# ─────────────────────────────────────────────────────────────
MAX_TARGET_DELTA = 0.45              # Monthly adaptive delta loop ceiling

# ─────────────────────────────────────────────────────────────
# 11. EXPECTED MOVE TARGETING (Intraday)
# Intraday: target strikes at 1.0x–1.15x EM for premium richness
# Monthly:  allow up to 1.5x EM (farther from spot)
# ─────────────────────────────────────────────────────────────
INTRADAY_EM_TARGET_MULT = 1.10        # Target zone: 1.0x–1.15x expected move
INTRADAY_EM_CAP_MULT = 1.50           # Hard cap: reject strikes beyond 1.5x EM intraday (was 1.0x — too tight, blocked most stocks)
INTRADAY_EM_HIGH_IV_CAP_MULT = 2.0   # High IV exception: allow up to 2x EM if IV > 35%
EXPECTED_MOVE_FLOOR_MULT = 1.0        # Monthly: physical floor (1x standard deviation)

# ─────────────────────────────────────────────────────────────
# 12. THETA VELOCITY SCORING
# intraday_theta_score = theta / (distance_from_spot_pct)
# Higher score = faster premium decay per unit of OTM risk
# ─────────────────────────────────────────────────────────────
MIN_INTRADAY_THETA_VELOCITY = 0.0010  # Minimum acceptable theta velocity score
MIN_DAILY_THETA_YIELD = 0.0005        # Monthly: Daily Theta Yield = Theta / Margin

# ─────────────────────────────────────────────────────────────
# 13. PREMIUM SYMMETRY TOLERANCE (Intraday — Relaxed)
# Allow asymmetric premium pairs as long as imbalance < 20%
# e.g. CE ₹12 / PE ₹9 → 25% diff → REJECTED
# e.g. CE ₹12 / PE ₹10 → 18% diff → ACCEPTED (theta velocity dominates)
# ─────────────────────────────────────────────────────────────
INTRADAY_PREMIUM_ASYMMETRY_TOLERANCE = 0.15   # 15% imbalance tolerance (was 20% — tighter for balanced strangles)

