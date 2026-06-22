"""
data_fetcher.py
Fetches OHLCV from Binance public API and computes all technical indicators
needed by the three trading skills (range-trading, trend-following, momentum-trading).
Funding rate / OI / L-S ratio come from Binance Futures public endpoints.
news and unusual_activity are left as empty lists — fill them via Co-Invest MCP.

Output: data/market_data.json
"""

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

ASSETS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

TIMEFRAME_CONFIG = {
    "1m":  {"interval": "1m",  "limit": 100},
    "15m": {"interval": "15m", "limit": 100},
    "1h":  {"interval": "1h",  "limit": 200},
    "4h":  {"interval": "4h",  "limit": 100},
    "1d":  {"interval": "1d",  "limit": 60},
}

BINANCE_SPOT    = "https://api.binance.com/api/v3"
BINANCE_FUT     = "https://fapi.binance.com/fapi/v1"
BINANCE_FUT_DATA = "https://fapi.binance.com/futures/data"


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


# ─── Binance fetchers ─────────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, limit: int) -> list:
    url = f"{BINANCE_SPOT}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return [
        {
            "open_time": k[0],
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        }
        for k in http_get(url)
    ]


def fetch_spot(symbol: str) -> dict:
    t = http_get(f"{BINANCE_SPOT}/ticker/24hr?symbol={symbol}")
    return {
        "price":          float(t["lastPrice"]),
        "change_24h_pct": float(t["priceChangePercent"]),
        "volume_24h":     float(t["quoteVolume"]),
        "high_24h":       float(t["highPrice"]),
        "low_24h":        float(t["lowPrice"]),
    }


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
                candles = fetch_klines(symbol, cfg["interval"], cfg["limit"])
                asset_data["timeframes"][tf] = candles
                asset_data["indicators"][tf] = compute_indicators(candles)
                print("ok")
            except Exception as e:
                print(f"ERROR: {e}")
                asset_data["timeframes"][tf] = []
                asset_data["indicators"][tf] = {}
            time.sleep(0.1)  # respect Binance rate limit

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
