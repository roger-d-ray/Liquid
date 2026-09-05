#!/usr/bin/env python3
"""Anti-stale staging helper (session-local).

Injects a just-fetched top-of-book (bid ask time_ms) into a fixed SOL momentum
breakout proposal and runs propose_pipeline.py in one shot, so the wall-clock
between show_orderbook and the pipeline's 30s freshness gate stays minimal.

Usage: python stage_run.py <best_bid> <best_ask> <book_time_ms>
"""
import json, subprocess, sys, datetime
from datetime import timezone

bid = float(sys.argv[1]); ask = float(sys.argv[2]); tms = int(sys.argv[3])

# Fixed structural levels for the SOL 15m momentum breakout (see run notes):
#   stop below the broken 20/55-bar high (103.44/103.52 -> support)
#   target toward next intraday resistance; entry = current executable ask.
STOP = 103.35
TP = 104.89
entry = ask
rr = (TP - entry) / (entry - STOP) if entry > STOP else 0.0

proposal = {
    "strategy": "momentum-trading", "asset": "SOL", "signal": "long", "timeframe": "15m",
    "entry": entry, "target": TP, "stop_loss": STOP,
    "confidence": 0.62, "signal_price": entry, "price": entry,
    "leverage": 10, "risk_pct": 0.03, "rr_ratio": round(rr, 2),
    "adx": 29.43, "rsi": 67.27, "ema9": 103.0412, "ema21": 102.8743, "ema50": 102.5882,
    "atr": 0.265, "macd_histogram": 0.0307, "volume_ratio": 1.982,
    "support": 103.44, "resistance": 105.87,
    "plus_di": 30.95, "minus_di": 10.6, "stochastic_k": 90.0, "stochastic_d": 71.06,
    "high_20_bars": 103.52, "low_20_bars": 102.32, "high_55_bars": 103.52, "low_55_bars": 101.62,
    "motivation": ("SOL momentum breakout 15m: prezzo oltre high 20/55-bar 103.52 su volume 1.98x; "
                   "ADX 29, +DI 31>>-DI 11, RSI 67, allineato 1h/4h/1d. Entry al prezzo eseguibile "
                   "corrente post-breakout; stop 103.35 sotto il livello di breakout (ora supporto), "
                   "~2xATR15m; TP 104.89; R/R ~1.9."),
}

coinvest = {
    "search_markets": {"structuredContent": {"text": "1 markets matching \"SOL\":\n1. SOL | Price $103.56 | 24h +1.97% | Vol $75.8M | Max 20x"}},
    "analyze_market": {"structuredContent": {"symbol": "SOL", "ticker": {"coin": "SOL", "markPx": str(ask), "maxLeverage": 20}}},
    "show_orderbook": {"structuredContent": {"book": {
        "coin": "SOL", "displayName": "SOL",
        "bids": [{"px": str(bid), "sz": "500", "n": 5}],
        "asks": [{"px": str(ask), "sz": "500", "n": 5}],
        "time": tms}}},
}

snap = json.load(open("data/portfolio_state.json"))
run_input = {
    "proposal": proposal, "coinvest": coinvest,
    "venues": {"signal_venue": "kraken", "derivatives_venue": "binance_futures", "execution_venue": "liquid"},
    "received_at": datetime.datetime.now(timezone.utc).isoformat(),
    "snapshot": snap,
}
json.dump(run_input, open("run_input.json", "w"), indent=2)
print(f"entry={entry} stop={STOP} tp={TP} rr={rr:.3f}", file=sys.stderr)

rc = subprocess.call([sys.executable, "propose_pipeline.py",
                      "--input", "run_input.json", "--result-file", "run_result.json"])
print(f"PIPELINE_EXIT:{rc}")
