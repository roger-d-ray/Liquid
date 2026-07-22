"""
data_fetcher.py
Fetches OHLCV from the Kraken public API (with Coinbase as automatic fallback)
and computes all technical indicators needed by the three trading skills
(range-trading, trend-following, momentum-trading).
Funding rate / OI / L-S ratio still come from Binance Futures public endpoints
(out of scope of the OHLCV migration; live data is normally enriched via the
Co-Invest MCP).
news and unusual_activity are left as empty lists — fill them via Co-Invest MCP.

Output: data/market_data.json
"""

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_bars import (
    NormalizedBars, normalize_ohlcv, only_available_closed_bars, utc_ms,
)

# ─── Config ──────────────────────────────────────────────────────────────────

ASSETS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

# Timeframes expressed as Coinbase granularities (seconds). Coinbase supports
# only {60, 300, 900, 3600, 21600, 86400}; 4h (14400) has no native granularity
# and is aggregated automatically from 1h candles (see fetch_ohlcv).
TIMEFRAME_CONFIG = {
    "1m":  {"granularity": 60,    "limit": 100},
    "15m": {"granularity": 900,   "limit": 100},
    "1h":  {"granularity": 3600,  "limit": 200},
    "4h":  {"granularity": 14400, "limit": 100},
    "1d":  {"granularity": 86400, "limit": 60},
}

REQUIRED_SIGNAL_TIMEFRAMES = ("15m", "1h")
REQUIRED_INDICATOR_KEYS = ("atr", "adx", "rsi", "ema9", "ema21", "ema50", "vol_ratio")
CLOSE_GRACE_SECONDS = float(os.getenv('OHLC_CLOSE_GRACE_SECONDS', '2'))
ROLLING_REFERENCE_MAX_DISTANCE_SECONDS = 30 * 60
ROLLING_REFERENCE_RULE = (
    'nearest timestamped hourly close to spot price timestamp minus 24h; '
    'ties prefer the earlier close; maximum distance is 30 minutes'
)
MARKET_DATA_SCHEMA_VERSION = 2

# ─── Coinbase OHLCV (public, no auth) — FALLBACK source ──────────────────────
# Coinbase rejects some datacenter IPs (HTTP 403), so it now sits behind Kraken
# as the automatic fallback; it works fine for local runs.
# The market-data candles endpoint is public: do NOT send COINBASE_API_KEY here.

COINBASE_BASE = "https://api.exchange.coinbase.com"
COINBASE_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
COINBASE_GRANULARITIES = {60, 300, 900, 3600, 21600, 86400}
COINBASE_MAX_CANDLES = 300  # hard limit per request

# Legacy Binance OHLCV endpoint — replaced by Coinbase, kept commented for ref.
# BINANCE_SPOT = "https://api.binance.com/api/v3"

# Binance Futures endpoints (funding / OI / L-S ratio) — NOT OHLCV, out of scope.
BINANCE_SPOT    = "https://api.binance.com/api/v3"
BINANCE_FUT     = "https://fapi.binance.com/fapi/v1"
BINANCE_FUT_DATA = "https://fapi.binance.com/futures/data"

# ─── Kraken OHLCV / spot (public, no auth) — PRIMARY source ──────────────────
# Kraken's public API does not geo-block datacenter/cloud IPs (unlike Coinbase
# 403 and Binance HTTP 451), so it is the default OHLCV/spot source and works in
# both local and cloud environments. Coinbase/Binance are kept as fallbacks.
KRAKEN_BASE = "https://api.kraken.com/0/public"
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
# granularity (seconds) -> Kraken OHLC interval (minutes). Kraken returns up to
# 720 candles per request, ascending (oldest→newest).
KRAKEN_INTERVALS = {60: 1, 300: 5, 900: 15, 1800: 30, 3600: 60, 14400: 240, 86400: 1440}

# Reverse lookup Binance-symbol -> asset key, used by the spot fallback.
SYMBOL_TO_ASSET = {v: k for k, v in ASSETS.items()}


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def http_get(url: str, retries: int = 3):
    req = urllib.request.Request(url, headers={"User-Agent": "liquid-bot/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def _local_now() -> datetime:
    return datetime.now(timezone.utc)


def _kraken_server_time() -> datetime:
    data = http_get(f"{KRAKEN_BASE}/Time")
    if data.get("error"):
        raise RuntimeError(f"Kraken Time: {data['error']}")
    return datetime.fromtimestamp(int(data["result"]["unixtime"]), tz=timezone.utc)


def _coinbase_server_time() -> datetime:
    data = http_get(f"{COINBASE_BASE}/time")
    return datetime.fromtimestamp(float(data["epoch"]), tz=timezone.utc)


def _venue_now(provider) -> datetime:
    """Prefer venue time; local UTC remains a safe availability fallback."""
    try:
        return provider()
    except Exception:
        return _local_now()


# ─── Coinbase OHLCV fetcher ───────────────────────────────────────────────────

def _coinbase_candles(product_id: str, granularity: int,
                      start: datetime, end: datetime, retries: int = 3) -> list:
    """Single Coinbase /candles request with retry+backoff on 429/5xx.

    Returns the raw array as Coinbase sends it: newest-first rows of
    [time, low, high, open, close, volume].
    """
    params = urllib.parse.urlencode({
        "granularity": granularity,
        "start": start.isoformat(),
        "end":   end.isoformat(),
    })
    url = f"{COINBASE_BASE}/products/{product_id}/candles?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "liquid-bot/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            transient = e.code == 429 or 500 <= e.code < 600
            if transient and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"Coinbase OHLCV {product_id} (gran={granularity}s): "
                f"HTTP {e.code} {e.reason}"
            ) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"Coinbase OHLCV {product_id} (gran={granularity}s): "
                f"errore di rete: {e}"
            ) from e


def _resample_plan(granularity_seconds: int):
    """For a non-native granularity, pick the largest native granularity that
    divides it. Returns (base_granularity, factor). Used for 4h (-> 1h x4)."""
    for base in sorted(COINBASE_GRANULARITIES, reverse=True):
        if granularity_seconds > base and granularity_seconds % base == 0:
            return base, granularity_seconds // base
    raise ValueError(
        f"Granularità {granularity_seconds}s non supportata da Coinbase "
        f"e non aggregabile da una granularità nativa"
    )


def _aggregate(
    base_candles: list, target_seconds: int, base_seconds: int | None = None
) -> list:
    """Aggregate ascending base candles into buckets of `target_seconds`,
    aligned to the UTC epoch (OHLC = first open, max high, min low, last close,
    summed volume)."""
    if base_seconds is None:
        base_seconds, _ = _resample_plan(target_seconds)
    if target_seconds % base_seconds:
        raise ValueError('target must be divisible by base interval')
    factor = target_seconds // base_seconds
    buckets: dict = {}
    for c in base_candles:
        ts = utc_ms(c['open_time']) // 1000
        if ts % base_seconds:
            raise ValueError(f'base candle {ts} is not UTC aligned')
        key = ts - (ts % target_seconds)
        buckets.setdefault(key, {})[ts] = c
    return [
        _aggregate_bucket(key, buckets[key], base_seconds, factor)
        for key in sorted(buckets)
    ]


def _aggregate_bucket(key: int, by_time: dict, base_seconds: int, factor: int) -> dict:
    expected = tuple(key + i * base_seconds for i in range(factor))
    parts = [by_time[ts] for ts in expected if ts in by_time]
    missing = [ts for ts in expected if ts not in by_time]
    if not parts:
        raise ValueError(f'aggregate bucket {key} has no aligned components')
    flags = ('missing_component_bars',) if missing else ()
    method = f'utc_aligned_{factor}x{base_seconds // 3600}h'
    return {
        'open_time': key * 1000,
        'open': parts[0]['open'],
        'high': max(part['high'] for part in parts),
        'low': min(part['low'] for part in parts),
        'close': parts[-1]['close'],
        'volume': sum(part['volume'] for part in parts),
        'aggregation_method': method,
        'completeness': 'incomplete' if missing else 'complete',
        'quality_flags': flags,
        'component_count': len(parts),
        'expected_component_count': factor,
        'missing_component_open_times': tuple(ts * 1000 for ts in missing),
    }


def _coinbase_raw_native(symbol: str, granularity_seconds: int,
                         num_candles: int) -> tuple[list, datetime, datetime]:
    """Native-granularity Coinbase fetch with pagination. Returns a chronological
    (oldest→newest) list of {open_time(ms), open, high, low, close, volume}."""
    product_id = COINBASE_PRODUCTS.get(symbol, symbol)
    collected: dict = {}                      # ts(seconds) -> candle dict (dedup)
    end = _venue_now(_coinbase_server_time)
    window = timedelta(seconds=granularity_seconds * COINBASE_MAX_CANDLES)
    max_pages = math.ceil(num_candles / COINBASE_MAX_CANDLES) + 2  # safety cap

    for _ in range(max_pages):
        if len(collected) >= num_candles:
            break
        start = end - window
        raw = _coinbase_candles(product_id, granularity_seconds, start, end)
        if not raw:
            break
        # Coinbase rows: [time, low, high, open, close, volume], newest-first.
        for row in raw:
            ts = int(row[0])
            collected[ts] = {
                "open_time": ts * 1000,        # ms, matching the old schema
                "open":   float(row[3]),
                "high":   float(row[2]),
                "low":    float(row[1]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            }
        oldest = min(int(row[0]) for row in raw)
        # Reuse the oldest boundary and deduplicate it. Subtracting one full
        # interval can skip the immediately preceding candle at page seams.
        end = datetime.fromtimestamp(oldest, tz=timezone.utc)
        time.sleep(0.2)  # respect Coinbase public rate limit (~10 req/s)

    # Reverse to chronological order and trim to the requested count.
    received_at = _local_now()
    observed_at = _venue_now(_coinbase_server_time)
    candles = [collected[k] for k in sorted(collected)]
    return candles[-num_candles:], observed_at, received_at


def _coinbase_ohlcv_native(symbol: str, granularity_seconds: int,
                           num_candles: int) -> NormalizedBars:
    rows, observed_at, received_at = _coinbase_raw_native(
        symbol, granularity_seconds, num_candles + 1
    )
    batch = normalize_ohlcv(
        rows, granularity_seconds, "coinbase",
        observed_at=observed_at,
        received_at=received_at,
        grace_period_seconds=CLOSE_GRACE_SECONDS,
    )
    return NormalizedBars(
        batch.closed[-num_candles:], batch.intrabar, batch.incomplete
    )


def _kraken_ohlc(symbol: str, granularity_seconds: int,
                 num_candles: int) -> NormalizedBars:
    """Native-granularity Kraken fetch (fallback). Kraken returns candles
    ascending already, as [time, open, high, low, close, vwap, volume, count]
    with time in seconds. Same output schema as the Coinbase fetcher."""
    interval = KRAKEN_INTERVALS.get(granularity_seconds)
    if interval is None:
        raise RuntimeError(
            f"Kraken: granularità {granularity_seconds}s non supportata "
            f"(intervalli validi: {sorted(KRAKEN_INTERVALS)})"
        )
    pair = KRAKEN_PAIRS.get(symbol, symbol)
    data = http_get(f"{KRAKEN_BASE}/OHLC?pair={pair}&interval={interval}")
    received_at = _local_now()
    observed_at = _venue_now(_kraken_server_time)
    if data.get("error"):
        raise RuntimeError(f"Kraken OHLC {pair}: {data['error']}")
    result = data["result"]
    key = next(k for k in result if k != "last")    # result key name varies
    rows = [{
        "open_time": int(r[0]) * 1000,
        "open":   float(r[1]),
        "high":   float(r[2]),
        "low":    float(r[3]),
        "close":  float(r[4]),
        "volume": float(r[6]),
    } for r in result[key]]
    batch = normalize_ohlcv(
        rows, granularity_seconds, "kraken",
        observed_at=observed_at,
        received_at=received_at,
        grace_period_seconds=CLOSE_GRACE_SECONDS,
    )
    return NormalizedBars(
        batch.closed[-num_candles:], batch.intrabar, batch.incomplete
    )


def _coinbase_ohlcv(symbol: str, granularity_seconds: int,
                    num_candles: int) -> NormalizedBars:
    """Coinbase OHLCV (fallback source). Coinbase has no native 4h granularity,
    so 4h (14400s) is aggregated from native 1h candles; native granularities are
    fetched directly with pagination."""
    if granularity_seconds not in COINBASE_GRANULARITIES:
        base, factor = _resample_plan(granularity_seconds)
        rows, observed_at, received_at = _coinbase_raw_native(
            symbol, base, num_candles * factor + factor
        )
        batch = normalize_ohlcv(
            _aggregate(rows, granularity_seconds),
            granularity_seconds,
            "coinbase",
            observed_at=observed_at,
            received_at=received_at,
            grace_period_seconds=CLOSE_GRACE_SECONDS,
        )
        return NormalizedBars(
            batch.closed[-num_candles:], batch.intrabar, batch.incomplete
        )
    return _coinbase_ohlcv_native(symbol, granularity_seconds, num_candles)


def fetch_ohlcv_batch(symbol: str, granularity_seconds: int,
                      num_candles: int) -> NormalizedBars:
    """Fetch `num_candles` OHLCV candles for `symbol` (BTC/ETH/SOL).

    Primary source is Kraken — its public API does not geo-block datacenter/cloud
    IPs, so it works in both local and cloud runs. On failure it transparently
    falls back to Coinbase (which rejects some cloud IPs with HTTP 403). Returns a
    chronological (oldest→newest) list of
    {open_time(ms), open, high, low, close, volume}.
    """
    try:
        return _kraken_ohlc(symbol, granularity_seconds, num_candles)
    except Exception as e:
        print(f"[fallback Coinbase: Kraken ko -> {e}]", end=" ", flush=True)
        return _coinbase_ohlcv(symbol, granularity_seconds, num_candles)


def fetch_ohlcv(symbol: str, granularity_seconds: int, num_candles: int) -> list:
    """Compatibility API returning only serialised ClosedBar values."""
    return [bar.to_dict() for bar in fetch_ohlcv_batch(
        symbol, granularity_seconds, num_candles
    ).closed]


def _timestamped_price(point: dict) -> tuple[int, float] | None:
    timestamp = next((
        point.get(key) for key in ('price_timestamp', 'timestamp', 'close_time')
        if point.get(key) is not None
    ), None)
    price = point.get('price', point.get('close'))
    if timestamp is None or price is None:
        return None
    return utc_ms(timestamp), float(price)


def select_rolling_24h_reference(
    current_timestamp, price_points: list,
    max_distance_seconds: int = ROLLING_REFERENCE_MAX_DISTANCE_SECONDS,
) -> dict | None:
    current_ms = utc_ms(current_timestamp)
    target_ms = current_ms - 24 * 60 * 60 * 1000
    candidates = [item for point in price_points if (item := _timestamped_price(point))]
    if not candidates:
        return None
    timestamp, price = min(
        candidates, key=lambda item: (
            abs(item[0] - target_ms), item[0] > target_ms, item[0],
        )
    )
    distance_ms = abs(timestamp - target_ms)
    if distance_ms > max_distance_seconds * 1000:
        return None
    return {
        'price': price,
        'timestamp': timestamp,
        'target_timestamp': target_ms,
        'distance_from_target_seconds': distance_ms / 1000,
        'actual_interval_seconds': (current_ms - timestamp) / 1000,
    }


def calculate_rolling_24h_change_pct(
    current_price: float, current_timestamp, price_points: list,
    max_distance_seconds: int = ROLLING_REFERENCE_MAX_DISTANCE_SECONDS,
) -> float | None:
    reference = select_rolling_24h_reference(
        current_timestamp, price_points, max_distance_seconds
    )
    if not reference or reference['price'] == 0:
        return None
    return round((float(current_price) / reference['price'] - 1) * 100, 6)


def calculate_change_since_utc_midnight_pct(
    current_price: float, current_timestamp, daily_bars: list,
) -> float | None:
    current_ms = utc_ms(current_timestamp)
    current_dt = datetime.fromtimestamp(current_ms / 1000, tz=timezone.utc)
    midnight_ms = utc_ms(current_dt.replace(hour=0, minute=0, second=0, microsecond=0))
    for bar in daily_bars:
        opened = bar.get('bar_open_time', bar.get('open_time'))
        if opened is not None and utc_ms(opened) == midnight_ms:
            open_price = float(bar['open'])
            return round((float(current_price) / open_price - 1) * 100, 6)
    return None


def _kraken_spot(asset: str) -> dict:
    """Spot snapshot from Kraken's public Ticker (fallback for fetch_spot).
    Kraken's 'o' is the UTC-day open. Its base volume is exact venue data;
    multiplication by the last price is exposed only as an estimate via
    estimated_quote_volume."""
    pair = KRAKEN_PAIRS.get(asset, asset)
    data = http_get(f"{KRAKEN_BASE}/Ticker?pair={pair}")
    if data.get("error"):
        raise RuntimeError(f"Kraken Ticker {pair}: {data['error']}")
    tk = next(iter(data["result"].values()))
    last  = float(tk["c"][0])                    # c = [last_price, lot_volume]
    open_ = float(tk["o"][0] if isinstance(tk["o"], list) else tk["o"])
    base_volume = float(tk['v'][1])
    observed_at = utc_ms(_venue_now(_kraken_server_time))
    return {
        'price': last,
        'price_timestamp': observed_at,
        'change_since_utc_midnight_pct': (
            round((last / open_ - 1) * 100, 6) if open_ else None
        ),
        'rolling_24h_change_pct': None,
        'base_volume': base_volume,
        'quote_volume': None,
        'estimated_quote_volume': base_volume * last,
        "high_24h":       float(tk["h"][1]),          # h[1] = 24h high
        "low_24h":        float(tk["l"][1]),          # l[1] = 24h low
        'source': 'kraken',
        'aggregation_method': 'kraken_ticker',
        'volume_window': 'rolling_24h',
        'completeness': 'partial',
        'quality_flags': [
            'quote_volume_estimated_from_base_volume_times_last_price'
        ],
    }


def _binance_spot(symbol: str) -> dict:
    """24h spot snapshot from Binance (fallback for fetch_spot)."""
    t = http_get(f'{BINANCE_SPOT}/ticker/24hr?symbol={symbol}')
    observed_at = utc_ms(t.get('closeTime', _local_now()))
    return {
        'price': float(t['lastPrice']),
        'price_timestamp': observed_at,
        'change_since_utc_midnight_pct': None,
        'rolling_24h_change_pct': None,
        'base_volume': float(t['volume']),
        'quote_volume': float(t['quoteVolume']),
        'estimated_quote_volume': None,
        "high_24h":       float(t["highPrice"]),
        "low_24h":        float(t["lowPrice"]),
        'source': 'binance',
        'aggregation_method': 'binance_24h_ticker',
        'volume_window': 'rolling_24h',
        'completeness': 'partial',
        'quality_flags': ['utc_midnight_change_unavailable_from_ticker'],
    }


def enrich_spot_with_history(
    spot: dict, hourly_bars: list, daily_bars: list = (),
) -> dict:
    result = dict(spot)
    flags = list(result.get('quality_flags', ()))
    reference = select_rolling_24h_reference(
        result['price_timestamp'], hourly_bars
    )
    result['rolling_24h_reference_rule'] = ROLLING_REFERENCE_RULE
    if reference:
        result['rolling_24h_change_pct'] = round(
            (result['price'] / reference['price'] - 1) * 100, 6
        )
        result['rolling_24h_reference_timestamp'] = reference['timestamp']
        result['rolling_24h_actual_interval_seconds'] = (
            reference['actual_interval_seconds']
        )
        result['rolling_24h_reference_distance_seconds'] = (
            reference['distance_from_target_seconds']
        )
    else:
        flags.append('rolling_24h_reference_missing_or_outside_tolerance')
    if result.get('change_since_utc_midnight_pct') is None:
        result['change_since_utc_midnight_pct'] = (
            calculate_change_since_utc_midnight_pct(
                result['price'], result['price_timestamp'], list(daily_bars)
            )
        )
    if result['change_since_utc_midnight_pct'] is None:
        flags.append('utc_midnight_reference_missing')
    else:
        flags = [f for f in flags if f != 'utc_midnight_change_unavailable_from_ticker']
    result['quality_flags'] = list(dict.fromkeys(flags))
    return result


def fetch_spot(symbol: str) -> dict:
    """Spot snapshot. Primary source is Kraken (cloud-reachable); falls back to
    Binance's 24h ticker if Kraken fails."""
    try:
        return _kraken_spot(SYMBOL_TO_ASSET.get(symbol, symbol))
    except Exception as e:
        print(f"[fallback Binance spot: Kraken ko -> {e}]", end=" ", flush=True)
        return _binance_spot(symbol)


def fetch_futures(symbol: str) -> dict:
    result = {
        "funding_rate": None,
        "oi": None,
        "long_pct": None,
        "short_pct": None,
        "derivatives_venue": "binance_futures",
    }
    try:
        p = http_get(f"{BINANCE_FUT}/premiumIndex?symbol={symbol}")
        result["funding_rate"] = float(p["lastFundingRate"])
    except Exception:
        pass
    try:
        o = http_get(f"{BINANCE_FUT}/openInterest?symbol={symbol}")
        result["oi"] = float(o["openInterest"])
    except Exception:
        pass
    try:
        ls = http_get(
            f"{BINANCE_FUT_DATA}/globalLongShortAccountRatio"
            f"?symbol={symbol}&period=5m&limit=1"
        )
        result["long_pct"]  = round(float(ls[0]["longAccount"])  * 100, 2)
        result["short_pct"] = round(float(ls[0]["shortAccount"]) * 100, 2)
    except Exception:
        pass
    return result


# ─── Indicator helpers ────────────────────────────────────────────────────────

def _c(candles): return [x["close"]  for x in candles]
def _h(candles): return [x["high"]   for x in candles]
def _l(candles): return [x["low"]    for x in candles]
def _v(candles): return [x["volume"] for x in candles]


def _ema_series(values: list, period: int) -> list:
    """Full EMA series, None where insufficient data."""
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    for v in values[period:]:
        out.append(out[-1] * (1 - k) + v * k)
    return out


def ema(values: list, period: int):
    s = _ema_series(values, period)
    return round(s[-1], 6) if s and s[-1] is not None else None


def sma(values: list, period: int):
    if len(values) < period:
        return None
    return round(sum(values[-period:]) / period, 6)


def rsi(closes: list, period: int = 14):
    if len(closes) < period + 1:
        return None
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [max(x, 0) for x in d]
    l = [max(-x, 0) for x in d]
    ag = sum(g[:period]) / period
    al = sum(l[:period]) / period
    for i in range(period, len(g)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al else 100.0


def atr(candles: list, period: int = 14):
    if len(candles) < period + 1:
        return None
    trs = [
        max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"]  - candles[i - 1]["close"]),
        )
        for i in range(1, len(candles))
    ]
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return round(val, 6)


def adx(candles: list, period: int = 14):
    if len(candles) < period * 2:
        return None
    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(candles)):
        h, l    = candles[i]["high"], candles[i]["low"]
        ph, pl  = candles[i - 1]["high"], candles[i - 1]["low"]
        pc      = candles[i - 1]["close"]
        up, dn  = h - ph, pl - l
        plus_dms.append(up if up > dn and up > 0 else 0)
        minus_dms.append(dn if dn > up and dn > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def wilder(s, p):
        v = sum(s[:p])
        r = [v]
        for x in s[p:]:
            v = v - v / p + x
            r.append(v)
        return r

    atr_w = wilder(trs, period)
    pdm_w = wilder(plus_dms, period)
    mdm_w = wilder(minus_dms, period)

    dxs, pdi_last, mdi_last = [], 0, 0
    for a, p, m in zip(atr_w, pdm_w, mdm_w):
        pdi = 100 * p / a if a else 0
        mdi = 100 * m / a if a else 0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
        pdi_last, mdi_last = pdi, mdi

    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return {
        "adx":      round(adx_val, 2),
        "plus_di":  round(pdi_last, 2),
        "minus_di": round(mdi_last, 2),
    }


def macd(closes: list, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    fast_s = _ema_series(closes, fast)
    slow_s = _ema_series(closes, slow)
    ml = [f - s for f, s in zip(fast_s, slow_s) if f is not None and s is not None]
    if len(ml) < signal:
        return None
    sig_s = _ema_series(ml, signal)
    mv, sv = ml[-1], sig_s[-1]
    return {
        "macd":      round(mv, 6),
        "signal":    round(sv, 6),
        "histogram": round(mv - sv, 6),
    }


def bollinger(closes: list, period=20, mult=2.0):
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((v - mid) ** 2 for v in w) / period)
    return {
        "upper":  round(mid + mult * std, 6),
        "middle": round(mid, 6),
        "lower":  round(mid - mult * std, 6),
        "std":    round(std, 6),
    }


def stochastic(candles: list, k_period=14, d_period=3):
    if len(candles) < k_period + d_period:
        return None
    closes = _c(candles)
    highs  = _h(candles)
    lows   = _l(candles)
    ks = []
    for i in range(k_period - 1, len(candles)):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i  - k_period + 1 : i + 1])
        ks.append(100 * (closes[i] - ll) / (hh - ll) if hh != ll else 50.0)
    return {"k": round(ks[-1], 2), "d": round(sum(ks[-d_period:]) / d_period, 2)}


def swing_levels(candles: list, lookback=20, *, as_of=None) -> dict:
    candles = only_available_closed_bars(candles, as_of=as_of or _local_now())
    w = candles[-lookback:]
    highs, lows = _h(w), _l(w)
    sh, sl = [], []
    for i in range(1, len(w) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            sh.append({"price": highs[i], "open_time": w[i]["open_time"]})
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            sl.append({"price": lows[i],  "open_time": w[i]["open_time"]})
    return {
        "swing_highs": sh,
        "swing_lows":  sl,
        "resistance":  max((x["price"] for x in sh), default=None),
        "support":     min((x["price"] for x in sl), default=None),
    }


def period_extreme(candles: list, n: int, *, as_of=None) -> dict:
    candles = only_available_closed_bars(candles, as_of=as_of or _local_now())
    w = candles[-n:] if len(candles) >= n else candles
    if not w:
        return {"high": None, "low": None}
    return {"high": max(_h(w)), "low": min(_l(w))}


# ─── Compute all indicators for one timeframe ─────────────────────────────────

def _are_utc_daily_bars(candles: list) -> bool:
    day_ms = 24 * 60 * 60 * 1000
    return all(
        utc_ms(c['open_time']) % day_ms == 0
        and utc_ms(c['close_time']) - utc_ms(c['open_time']) == day_ms
        for c in candles
    )


def compute_indicators(
    candles: list, *, as_of=None, timeframe: str | None = None
) -> dict:
    as_of = as_of or _local_now()
    candles = only_available_closed_bars(candles, as_of=as_of)
    if not candles:
        raise ValueError("indicators require at least one available ClosedBar")
    closes  = _c(candles)
    volumes = _v(candles)
    vol_avg = sma(volumes, 20)
    adx_res = adx(candles)
    swings  = swing_levels(candles, lookback=20, as_of=as_of)

    ind = {
        # EMAs
        "ema9":   ema(closes, 9),
        "ema21":  ema(closes, 21),
        "ema50":  ema(closes, 50),
        "ema200": ema(closes, 200),
        # RSI
        "rsi": rsi(closes, 14),
        # MACD
        "macd": macd(closes),
        # Bollinger
        "bollinger": bollinger(closes),
        # Stochastic
        "stochastic": stochastic(candles),
        # ATR
        "atr": atr(candles, 14),
        # ADX
        "adx":      adx_res["adx"]      if adx_res else None,
        "plus_di":  adx_res["plus_di"]  if adx_res else None,
        "minus_di": adx_res["minus_di"] if adx_res else None,
        # Volume
        "vol_sma20":  vol_avg,
        "vol_ratio":  round(volumes[-1] / vol_avg, 3) if vol_avg else None,
        # Swing structure
        "swing_highs": swings["swing_highs"],
        "swing_lows":  swings["swing_lows"],
        "resistance":  swings["resistance"],
        "support":     swings["support"],
        # Breakout levels
        'high_20_bars': period_extreme(candles, 20, as_of=as_of)['high'],
        'low_20_bars': period_extreme(candles, 20, as_of=as_of)['low'],
        'high_55_bars': period_extreme(candles, 55, as_of=as_of)['high']
        if len(candles) >= 55 else None,
        'low_55_bars': period_extreme(candles, 55, as_of=as_of)['low']
        if len(candles) >= 55 else None,
    }
    if timeframe == '1d':
        if not _are_utc_daily_bars(candles):
            raise ValueError('daily levels require UTC-aligned 1d bars')
        daily = period_extreme(candles, 20, as_of=as_of)
        ind['high_20_days'] = daily['high']
        ind['low_20_days'] = daily['low']
    return ind


# ─── Main ─────────────────────────────────────────────────────────────────────

def validate_signal_ready_output(output: dict) -> None:
    """Fail fast when JSON is not usable for the 15m/1h trading routine."""
    errors = []
    output_time = output.get("timestamp") or _local_now()
    if isinstance(output_time, str):
        output_time = datetime.fromisoformat(output_time.replace("Z", "+00:00"))
    output_time_ms = (
        int(output_time.timestamp() * 1000)
        if isinstance(output_time, datetime)
        else int(output_time)
    )
    for asset in ASSETS:
        asset_data = output.get("assets", {}).get(asset) or {}
        live = asset_data.get("live") or {}
        if live.get("price") is None:
            errors.append(f"{asset}: live.price mancante")

        for tf in REQUIRED_SIGNAL_TIMEFRAMES:
            candles = asset_data.get("timeframes", {}).get(tf) or []
            indicators = asset_data.get("indicators", {}).get(tf) or {}
            if not candles:
                errors.append(f"{asset} {tf}: candles mancanti")
                continue
            if any(c.get('completeness', 'complete') != 'complete' for c in candles):
                errors.append(f'{asset} {tf}: presenti barre incomplete')
                continue
            if any(c.get("is_final") is not True or c.get("available_at") is None
                   for c in candles):
                errors.append(f"{asset} {tf}: presenti dati non ClosedBar")
                continue
            if any(int(c["available_at"]) > output_time_ms for c in candles):
                errors.append(f"{asset} {tf}: ClosedBar non ancora disponibile")
                continue
            if not indicators:
                errors.append(f"{asset} {tf}: indicatori mancanti")
                continue
            missing_keys = [k for k in REQUIRED_INDICATOR_KEYS if k not in indicators]
            if missing_keys:
                errors.append(
                    f"{asset} {tf}: indicatori senza chiavi {', '.join(missing_keys)}"
                )

    if errors:
        raise RuntimeError(
            "market_data.json non pronto per segnali intraday: " + "; ".join(errors)
        )


def main():
    output = {
        'schema_version': MARKET_DATA_SCHEMA_VERSION,
        'field_migrations': {
            'high_20': 'high_20_bars',
            'low_20': 'low_20_bars',
            'high_55': 'high_55_bars',
            'low_55': 'low_55_bars',
            'change_24h_pct': [
                'change_since_utc_midnight_pct',
                'rolling_24h_change_pct',
            ],
            'volume_24h': [
                'base_volume', 'quote_volume', 'estimated_quote_volume',
            ],
        },
        "timestamp": _local_now().isoformat(),
        "assets": {},
    }

    for asset, symbol in ASSETS.items():
        print(f"\n[{asset}]")
        asset_data = {
            "timeframes": {},
            "intrabar": {},
            "incomplete": {},
            "indicators": {},
            "live": {},
            "news": [],
            "unusual_activity": [],
        }

        for tf, cfg in TIMEFRAME_CONFIG.items():
            print(f"  {tf} ({cfg['limit']} candles)...", end=" ", flush=True)
            try:
                batch = fetch_ohlcv_batch(asset, cfg["granularity"], cfg["limit"])
                candles = [bar.to_dict() for bar in batch.closed]
                asset_data["timeframes"][tf] = candles
                asset_data["intrabar"][tf] = [bar.to_dict() for bar in batch.intrabar]
                asset_data['incomplete'][tf] = [
                    bar.to_dict() for bar in batch.incomplete
                ]
                asset_data['indicators'][tf] = compute_indicators(
                    candles, timeframe=tf
                )
                print("ok")
            except Exception as e:
                print(f"ERROR: {e}")
                asset_data["timeframes"][tf] = []
                asset_data["intrabar"][tf] = []
                asset_data['incomplete'][tf] = []
                asset_data["indicators"][tf] = {}
            time.sleep(0.1)  # respect Coinbase rate limit

        print("  live + futures...", end=" ", flush=True)
        try:
            spot = enrich_spot_with_history(
                fetch_spot(symbol),
                asset_data['timeframes'].get('1h', []),
                asset_data['intrabar'].get('1d', []),
            )
            futures = fetch_futures(symbol)
            live = {**spot, **futures}
            live["signal_venue"] = spot.get("source")
            asset_data["live"] = live
            print("ok")
        except Exception as e:
            print(f"ERROR: {e}")

        output["assets"][asset] = asset_data

    output["timestamp"] = _local_now().isoformat()
    validate_signal_ready_output(output)

    out_path = Path(__file__).parent / "data" / "market_data.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nSalvato in {out_path}")


if __name__ == "__main__":
    main()
