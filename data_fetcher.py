"""
data_fetcher.py
Fetches OHLCV from the Coinbase Exchange public API and computes all technical
indicators needed by the three trading skills (range-trading, trend-following,
momentum-trading).
Funding rate / OI / L-S ratio still come from Binance Futures public endpoints
(out of scope of the OHLCV migration; live data is normally enriched via the
Co-Invest MCP).
news and unusual_activity are left as empty lists — fill them via Co-Invest MCP.

Output: data/market_data.json
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# ─── Coinbase OHLCV (public, no auth) ────────────────────────────────────────
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

# ─── Kraken fallback (public, no auth) ───────────────────────────────────────
# Coinbase and Binance both reject datacenter/cloud IPs (Coinbase 403, Binance
# HTTP 451). Kraken's public API does not geo-block cloud IPs, so it is used as
# an automatic fallback when the primary source fails. Local runs keep using
# Coinbase; cloud runs transparently fall back here.
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


def _aggregate(base_candles: list, target_seconds: int) -> list:
    """Aggregate ascending base candles into buckets of `target_seconds`,
    aligned to the UTC epoch (OHLC = first open, max high, min low, last close,
    summed volume)."""
    buckets: dict = {}
    for c in base_candles:
        ts = c["open_time"] // 1000
        key = ts - (ts % target_seconds)
        b = buckets.get(key)
        if b is None:
            buckets[key] = {
                "open_time": key * 1000,
                "open":   c["open"],
                "high":   c["high"],
                "low":    c["low"],
                "close":  c["close"],
                "volume": c["volume"],
            }
        else:
            b["high"]    = max(b["high"], c["high"])
            b["low"]     = min(b["low"],  c["low"])
            b["close"]   = c["close"]
            b["volume"] += c["volume"]
    return [buckets[k] for k in sorted(buckets)]


def _coinbase_ohlcv_native(symbol: str, granularity_seconds: int,
                           num_candles: int) -> list:
    """Native-granularity Coinbase fetch with pagination. Returns a chronological
    (oldest→newest) list of {open_time(ms), open, high, low, close, volume}."""
    product_id = COINBASE_PRODUCTS.get(symbol, symbol)
    collected: dict = {}                      # ts(seconds) -> candle dict (dedup)
    end = datetime.now(timezone.utc)
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
        end = datetime.fromtimestamp(oldest, tz=timezone.utc) \
            - timedelta(seconds=granularity_seconds)
        time.sleep(0.2)  # respect Coinbase public rate limit (~10 req/s)

    # Reverse to chronological order and trim to the requested count.
    candles = [collected[k] for k in sorted(collected)]
    return candles[-num_candles:]


def _kraken_ohlc(symbol: str, granularity_seconds: int, num_candles: int) -> list:
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
    if data.get("error"):
        raise RuntimeError(f"Kraken OHLC {pair}: {data['error']}")
    result = data["result"]
    key = next(k for k in result if k != "last")    # result key name varies
    candles = [{
        "open_time": int(r[0]) * 1000,
        "open":   float(r[1]),
        "high":   float(r[2]),
        "low":    float(r[3]),
        "close":  float(r[4]),
        "volume": float(r[6]),
    } for r in result[key]]
    return candles[-num_candles:]


def fetch_ohlcv(symbol: str, granularity_seconds: int, num_candles: int) -> list:
    """Fetch `num_candles` OHLCV candles for `symbol` (BTC/ETH/SOL).

    Primary source is Coinbase; on failure (e.g. cloud IPs rejected with 403) it
    transparently falls back to Kraken. Returns a chronological (oldest→newest)
    list of {open_time(ms), open, high, low, close, volume}.

    Non-native granularities (e.g. 4h = 14400s) are aggregated from the largest
    native granularity that divides them — the fallback happens at the native
    level, so aggregation works regardless of which source served the candles.
    """
    if granularity_seconds not in COINBASE_GRANULARITIES:
        base, factor = _resample_plan(granularity_seconds)
        base_candles = fetch_ohlcv(symbol, base, num_candles * factor)
        return _aggregate(base_candles, granularity_seconds)[-num_candles:]

    try:
        return _coinbase_ohlcv_native(symbol, granularity_seconds, num_candles)
    except Exception as e:
        print(f"[fallback Kraken: Coinbase ko -> {e}]", end=" ", flush=True)
        return _kraken_ohlc(symbol, granularity_seconds, num_candles)


def _kraken_spot(asset: str) -> dict:
    """Spot snapshot from Kraken's public Ticker (fallback for fetch_spot).
    Note: Kraken's 'o' is the *current day* open, so change_24h_pct is an
    approximation of intraday change; volume is converted to quote (USD) via
    last price. Good enough for the live snapshot when Binance is blocked."""
    pair = KRAKEN_PAIRS.get(asset, asset)
    data = http_get(f"{KRAKEN_BASE}/Ticker?pair={pair}")
    if data.get("error"):
        raise RuntimeError(f"Kraken Ticker {pair}: {data['error']}")
    tk = next(iter(data["result"].values()))
    last  = float(tk["c"][0])                    # c = [last_price, lot_volume]
    open_ = float(tk["o"][0] if isinstance(tk["o"], list) else tk["o"])
    return {
        "price":          last,
        "change_24h_pct": round((last - open_) / open_ * 100, 3) if open_ else None,
        "volume_24h":     float(tk["v"][1]) * last,   # v[1] = 24h base volume
        "high_24h":       float(tk["h"][1]),          # h[1] = 24h high
        "low_24h":        float(tk["l"][1]),          # l[1] = 24h low
    }


def fetch_spot(symbol: str) -> dict:
    try:
        t = http_get(f"{BINANCE_SPOT}/ticker/24hr?symbol={symbol}")
        return {
            "price":          float(t["lastPrice"]),
            "change_24h_pct": float(t["priceChangePercent"]),
            "volume_24h":     float(t["quoteVolume"]),
            "high_24h":       float(t["highPrice"]),
            "low_24h":        float(t["lowPrice"]),
        }
    except Exception as e:
        print(f"[fallback Kraken spot: Binance ko -> {e}]", end=" ", flush=True)
        return _kraken_spot(SYMBOL_TO_ASSET.get(symbol, symbol))


def fetch_futures(symbol: str) -> dict:
    result = {"funding_rate": None, "oi": None, "long_pct": None, "short_pct": None}
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


def swing_levels(candles: list, lookback=20) -> dict:
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


def period_extreme(candles: list, n: int) -> dict:
    w = candles[-n:] if len(candles) >= n else candles
    return {"high": max(_h(w)), "low": min(_l(w))}


# ─── Compute all indicators for one timeframe ─────────────────────────────────

def compute_indicators(candles: list) -> dict:
    closes  = _c(candles)
    volumes = _v(candles)
    vol_avg = sma(volumes, 20)
    adx_res = adx(candles)
    swings  = swing_levels(candles, lookback=20)

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
        "high_20": period_extreme(candles, 20)["high"],
        "low_20":  period_extreme(candles, 20)["low"],
        "high_55": period_extreme(candles, 55)["high"] if len(candles) >= 55 else None,
        "low_55":  period_extreme(candles, 55)["low"]  if len(candles) >= 55 else None,
    }
    return ind


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "assets": {},
    }

    for asset, symbol in ASSETS.items():
        print(f"\n[{asset}]")
        asset_data = {
            "timeframes": {},
            "indicators": {},
            "live": {},
            "news": [],
            "unusual_activity": [],
        }

        for tf, cfg in TIMEFRAME_CONFIG.items():
            print(f"  {tf} ({cfg['limit']} candles)...", end=" ", flush=True)
            try:
                candles = fetch_ohlcv(asset, cfg["granularity"], cfg["limit"])
                asset_data["timeframes"][tf] = candles
                asset_data["indicators"][tf] = compute_indicators(candles)
                print("ok")
            except Exception as e:
                print(f"ERROR: {e}")
                asset_data["timeframes"][tf] = []
                asset_data["indicators"][tf] = {}
            time.sleep(0.1)  # respect Coinbase rate limit

        print("  live + futures...", end=" ", flush=True)
        try:
            live = {**fetch_spot(symbol), **fetch_futures(symbol)}
            asset_data["live"] = live
            print("ok")
        except Exception as e:
            print(f"ERROR: {e}")

        output["assets"][asset] = asset_data

    out_path = Path(__file__).parent / "data" / "market_data.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nSalvato in {out_path}")


if __name__ == "__main__":
    main()
