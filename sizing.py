"""
sizing.py
Risk-based position sizing for the intraday policy (CLAUDE.md STEP 5).

The amount to invest is derived from RISK, not from leverage:

    risk_usd   = risk_pct * equity          # what you LOSE if the stop is hit
    size_coin  = risk_usd / |entry - stop|  # position size in coin units
    size_usd   = size_coin * entry          # notional — the "investito"
    margin_usd = size_usd / leverage        # capital actually locked as margin

Leverage does NOT change risk_usd: it only sets the margin committed and the
liquidation distance. This is the single source of truth for sizing so the number
shown in the Telegram proposal is exactly the number passed to execute_order().
"""

from __future__ import annotations


def compute_size(equity: float, risk_pct: float, entry: float,
                 stop_loss: float, leverage: float = 1.0) -> dict:
    """Return a sizing breakdown for a proposal.

    Raises ValueError on inputs that make sizing impossible (non-positive equity/
    entry, zero stop distance, non-positive risk_pct).
    """
    equity   = float(equity)
    risk_pct = float(risk_pct)
    entry    = float(entry)
    stop     = float(stop_loss)
    lev      = float(leverage) if leverage else 1.0

    if equity <= 0:
        raise ValueError("equity deve essere > 0")
    if not (0 < risk_pct < 1):
        raise ValueError("risk_pct deve essere una frazione tra 0 e 1 (es. 0.04 = 4%)")
    if entry <= 0:
        raise ValueError("entry deve essere > 0")
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        raise ValueError("distanza di stop nulla: entry e stop_loss coincidono")
    if lev <= 0:
        lev = 1.0

    risk_usd   = risk_pct * equity
    size_coin  = risk_usd / stop_dist
    size_usd   = size_coin * entry
    margin_usd = size_usd / lev

    return {
        "equity":        round(equity, 2),
        "risk_pct":      round(risk_pct, 4),
        "risk_usd":      round(risk_usd, 2),
        "size_coin":     round(size_coin, 8),
        "size_usd":      round(size_usd, 2),
        "margin_usd":    round(margin_usd, 2),
        "leverage":      lev,
        "stop_dist":     round(stop_dist, 8),
        "stop_dist_pct": round(stop_dist / entry * 100, 3),
    }


if __name__ == "__main__":
    import json
    import sys
    # CLI: python sizing.py equity risk_pct entry stop_loss leverage
    if len(sys.argv) != 6:
        print("Usage: python sizing.py <equity> <risk_pct> <entry> <stop_loss> <leverage>")
        sys.exit(1)
    eq, rp, en, sl, lv = map(float, sys.argv[1:6])
    print(json.dumps(compute_size(eq, rp, en, sl, lv), indent=2))
