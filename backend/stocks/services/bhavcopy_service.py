"""
Fetch NSE bhavcopy CSV for a given date, parse it, and bulk-upsert Stock rows.

URL format:
  https://nsearchives.nseindia.com/content/cm/
  BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
"""

import io
import logging
import zipfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd
import requests
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

# ── NSE headers (required to bypass 403) ────────────────────────
_NSE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.nseindia.com/',
}

# Actual columns in the new NSE bhavcopy CSV:
# TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb,
# SctySrs, OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric,
# OpnIntrst, ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd, ...
#
# NOTE: DlvryPct is NOT in this CSV. Delivery data comes from a separate report.

# Series filter column
_SERIES_COL = 'SctySrs'


# Segment-specific URL builds
def _build_url(target_date: date, segment: str = 'cm') -> str:
    """Return the bhavcopy ZIP URL for *target_date* and *segment*."""
    if segment == 'fo':
        return settings.NSE_FO_BHAVCOPY_URL.format(yyyymmdd=target_date.strftime('%Y%m%d'))
    return settings.NSE_BHAVCOPY_URL.format(yyyymmdd=target_date.strftime('%Y%m%d'))


def _get_session() -> requests.Session:
    """Return a requests.Session pre-loaded with NSE cookies."""
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
    # Hit main page first to obtain cookies
    session.get(settings.NSE_BASE_URL, timeout=10)
    return session


def _download_csv(session: requests.Session, url: str) -> pd.DataFrame:
    """Download the ZIP, extract the CSV, return a DataFrame."""
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as csv_file:
            df = pd.read_csv(csv_file)

    # Normalise column names (strip whitespace)
    df.columns = df.columns.str.strip()
    return df


def _safe_decimal(value, default=None) -> Decimal | None:
    """Convert a value to Decimal, returning *default* on failure."""
    if pd.isna(value):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


_MAX_LOOKBACK_DAYS = 7  # Max days to walk back when bhavcopy not found

# Narrowed NIFTY500 -> NIFTY50 2026-07-28, then widened NIFTY50 -> NIFTY100 at the
# account owner's request — the whole platform (intraday, option buying, specialist,
# short-term, long-term) now scans a single shared NIFTY100 universe. Function name
# below kept as "nifty500" to avoid touching every call site across
# trade_engine.py / pro_system_service.py / truedata_service.py.


def _fetch_nifty500_symbols(session: requests.Session) -> set[str]:
    """Delegates to signal_utils.fetch_nifty_symbols_live — the one shared live-fetch
    +cache every engine now reads the trading universe through (currently NIFTY100),
    so a reconstitution (symbol added/removed from the index) lands here the same as
    everywhere else. `session` is unused but kept so existing call sites don't need
    to change.
    """
    from stocks.services.signal_utils import fetch_nifty_symbols_live

    return set(fetch_nifty_symbols_live("NIFTY100"))


def _fetch_delivery_data(session: requests.Session, target_date: date) -> dict[str, Decimal]:
    """
    Fetch the sec_bhavdata_full CSV for *target_date* and return
    a dict of {symbol: delivery_percentage}.

    Returns empty dict on failure (non-critical).
    """
    url = settings.NSE_DELIVERY_URL.format(ddmmyyyy=target_date.strftime('%d%m%Y'))
    logger.info("Fetching delivery data from %s", url)
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            logger.warning("Delivery data not available (HTTP %d)", resp.status_code)
            return {}

        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = df.columns.str.strip()

        # Filter equity series
        if 'SERIES' in df.columns:
            df = df[df['SERIES'].str.strip() == 'EQ']

        result = {}
        for _, row in df.iterrows():
            symbol = str(row.get('SYMBOL', '')).strip()
            deliv_pct = _safe_decimal(row.get('DELIV_PER'))
            if symbol and deliv_pct is not None:
                result[symbol] = deliv_pct

        logger.info("Fetched delivery data for %d symbols", len(result))
        return result
    except Exception as exc:
        logger.warning("Failed to fetch delivery data: %s", exc)
        return {}


def _fetch_fo_oi_data(session: requests.Session, target_date: date) -> dict[str, tuple[int, int]]:
    """
    Fetch the FO bhavcopy for *target_date* and aggregate Open Interest (OI)
    and Change in OI for each symbol.

    Returns dict mapping symbol -> (total_oi, total_oi_change).
    Summing all expiries and instrument types (FUTSTK, OPTSTK, etc).
    """
    url = _build_url(target_date, segment='fo')
    logger.info("Fetching FO OI data from %s", url)
    try:
        df = _download_csv(session, url)
        # Instrument mapping (some FO symbols differ from CM)
        # e.g. NIFTY -> NIFTY 50
        mapping = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK'}
        
        # Aggregate by symbol
        agg = df.groupby('TckrSymb').agg({
            'OpnIntrst': 'sum',
            'ChngInOpnIntrst': 'sum'
        })
        
        result = {}
        for symbol, row in agg.iterrows():
            clean_sym = str(symbol).strip()
            # Map index symbols if needed
            clean_sym = mapping.get(clean_sym, clean_sym)
            result[clean_sym] = (int(row['OpnIntrst']), int(row['ChngInOpnIntrst']))
            
        logger.info("Aggregated FO OI data for %d symbols", len(result))
        return result
    except Exception as exc:
        logger.warning("Failed to fetch FO OI data: %s", exc)
        return {}


def _find_available_date(session: requests.Session, target_date: date) -> tuple[date, pd.DataFrame]:
    """
    Try *target_date* first; on 404 walk back up to _MAX_LOOKBACK_DAYS
    (skipping weekends) to find the most recent available bhavcopy.

    Returns (actual_date, dataframe).
    Raises requests.HTTPError if nothing is found within the window.
    """
    attempt = target_date
    for _ in range(_MAX_LOOKBACK_DAYS):
        url = _build_url(attempt, segment='cm')
        logger.info("Trying bhavcopy for %s → %s", attempt, url)
        resp = session.get(url, timeout=30)

        if resp.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                csv_name = zf.namelist()[0]
                with zf.open(csv_name) as csv_file:
                    df = pd.read_csv(csv_file)
            df.columns = df.columns.str.strip()
            if attempt != target_date:
                logger.info("Bhavcopy not available for %s, using %s instead",
                            target_date, attempt)
            return attempt, df

        if resp.status_code == 404:
            # Walk back one day, skip weekends
            attempt -= timedelta(days=1)
            while attempt.weekday() >= 5:  # 5=Sat, 6=Sun
                attempt -= timedelta(days=1)
            continue

        # Any other HTTP error — raise immediately
        resp.raise_for_status()

    raise requests.HTTPError(
        f"No bhavcopy found for {target_date} or the previous "
        f"{_MAX_LOOKBACK_DAYS} trading days."
    )


def fetch_and_store_bhavcopy(target_date: date) -> tuple[int, date]:
    """
    Download the NSE bhavcopy for *target_date*, filter equity rows,
    and bulk-upsert into the Stock table.

    If the bhavcopy for *target_date* is not yet available (404),
    walks back up to 7 trading days to find the most recent one.

    Returns (rows_upserted, actual_date) — the actual trading date
    may differ from *target_date* on holidays/weekends.
    """
    session = _get_session()
    actual_date, df = _find_available_date(session, target_date)
    target_date = actual_date

    # Fetch the platform's trading universe symbols, delivery data, and FO OI data
    # (reuse session) — despite the function name, this is NIFTY100 now, not NIFTY50.
    universe_symbols = _fetch_nifty500_symbols(session)
    delivery_map = _fetch_delivery_data(session, target_date)
    oi_map = _fetch_fo_oi_data(session, target_date)

    # Keep only equity series
    if _SERIES_COL in df.columns:
        df = df[df[_SERIES_COL].str.strip() == 'EQ']

    logger.info("Parsed %d EQ rows for %s", len(df), target_date)

    # ── Filter: only trading-universe stocks ────────────────
    before = len(df)
    if universe_symbols and 'TckrSymb' in df.columns:
        df = df[df['TckrSymb'].str.strip().isin(universe_symbols)]
    logger.info("Filtered %d → %d stocks (trading universe only)", before, len(df))

    # Compute price_change_percent from ClsPric & PrvsClsgPric
    if {'ClsPric', 'PrvsClsgPric'}.issubset(df.columns):
        df['_price_change_pct'] = (
            (df['ClsPric'] - df['PrvsClsgPric']) / df['PrvsClsgPric'] * 100
        )
    else:
        df['_price_change_pct'] = None

    # NOTE: Local Saving for Stock table is currently disabled as per 'Nuclear Cleanup' directive.
    total = len(df)
    logger.info("Bypassed DB upsert for %d stocks for %s (Local Saving Disabled)",
                total, target_date)
    return total, target_date
