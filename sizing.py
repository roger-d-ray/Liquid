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

import math


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


def _liq_safe_leverage(stop_dist_pct: float, hard_max: float,
                       liq_buffer: float = 1.25) -> float:
    """Leverage ceiling that keeps the liquidation price beyond the stop.

    Liquidation distance ≈ 1/leverage (as a fraction of price). We require it to
    be at least `liq_buffer`× the stop distance, so raising leverage to fit the
    margin can never place the liquidation before the stop (which would defeat the
    stop and blow up the risk). Returns min(hard_max, liq-safe leverage).
    For tight intraday stops this is far above 20x, so MAX_LEVERAGE binds; it only
    bites on wide stops.
    """
    stop_frac = stop_dist_pct / 100.0
    if stop_frac <= 0:
        return hard_max
    return min(hard_max, 1.0 / (stop_frac * liq_buffer))


def fit_leverage_to_margin(equity: float, available_balance, risk_pct: float,
                           entry: float, stop_loss: float, leverage: float,
                           existing_total_margin: float = 0.0,
                           existing_asset_margin: float = 0.0,
                           max_leverage: float = None,
                           max_total_margin_pct: float = None,
                           max_per_asset_margin_pct: float = None) -> dict:
    """Size a trade and, if its margin doesn't fit, RAISE leverage (up to the
    cap) before giving up — then discard if it still doesn't fit.

    The notional is fixed by risk-based sizing (independent of leverage), so
    margin = notional / leverage: raising leverage lowers margin without changing
    the € risk. We compute the minimum leverage that fits inside the tightest of
    three budgets (per-asset cap, total cap, available balance), bounded by the
    liquidation-safe ceiling.

    Returns the compute_size() dict plus: fits (bool), leverage (final),
    requested_leverage, adjusted (bool), notional, margin_budget, and a human
    `reason`. Caps default to risk_manager's (single source of truth).
    """
    from risk_manager import (MAX_LEVERAGE, MAX_TOTAL_MARGIN_PCT,
                              MAX_PER_ASSET_MARGIN_PCT)
    max_leverage             = MAX_LEVERAGE if max_leverage is None else max_leverage
    max_total_margin_pct     = MAX_TOTAL_MARGIN_PCT if max_total_margin_pct is None else max_total_margin_pct
    max_per_asset_margin_pct = MAX_PER_ASSET_MARGIN_PCT if max_per_asset_margin_pct is None else max_per_asset_margin_pct

    base       = compute_size(equity, risk_pct, entry, stop_loss, leverage)
    notional   = base["size_usd"]
    stop_pct   = base["stop_dist_pct"]

    existing_total_margin = float(existing_total_margin or 0.0)
    existing_asset_margin = float(existing_asset_margin or 0.0)

    budget_total = max_total_margin_pct * equity - existing_total_margin
    budget_asset = max_per_asset_margin_pct * equity - existing_asset_margin
    budget_avail = float(available_balance) if available_balance is not None else budget_total
    budget       = min(budget_total, budget_asset, budget_avail)

    lev_ceiling = _liq_safe_leverage(stop_pct, max_leverage)

    out = {
        **base,
        "requested_leverage": float(leverage),
        "notional":           round(notional, 2),
        "margin_budget":      round(budget, 2),
        "leverage_ceiling":   round(lev_ceiling, 2),
    }

    if budget <= 0:
        out.update(fits=False, adjusted=False,
                   reason=("Cap di margine già saturi dalle posizioni aperte: "
                           "nessun budget per un nuovo trade."))
        return out

    lev_needed = notional / budget
    final_lev  = math.ceil(max(float(leverage), lev_needed))  # integer, extra headroom

    if final_lev > lev_ceiling + 1e-9:
        margin_at_ceiling = notional / lev_ceiling
        out.update(compute_size(equity, risk_pct, entry, stop_loss, lev_ceiling))
        out.update(fits=False, adjusted=False, leverage=round(lev_ceiling, 2),
                   reason=(f"Margine troppo alto anche alla leva massima utile "
                           f"({lev_ceiling:.0f}x): servono ~${margin_at_ceiling:,.0f} "
                           f"ma il budget è ${budget:,.0f}. Trade scartato."))
        return out

    adjusted = final_lev > float(leverage) + 1e-9
    out.update(compute_size(equity, risk_pct, entry, stop_loss, final_lev))
    out.update(fits=True, adjusted=adjusted, leverage=float(final_lev),
               requested_leverage=float(leverage),
               notional=round(notional, 2), margin_budget=round(budget, 2),
               leverage_ceiling=round(lev_ceiling, 2))
    out["reason"] = (
        f"Leva alzata da {leverage:g}x a {final_lev:g}x per rientrare nel margine "
        f"(margine ${out['margin_usd']:,.0f} ≤ budget ${budget:,.0f})."
        if adjusted else
        f"Rientra nel margine alla leva richiesta ({leverage:g}x)."
    )
    return out


def plan_trade(proposal: dict, snapshot: dict) -> dict:
    """High-level helper for the routine's STEP 4-BIS: size a proposal and fit its
    leverage against the current portfolio snapshot in one call.

    Reads equity/available/positions from the snapshot and entry/stop/leverage/
    risk_pct from the proposal (tolerant of sl/stop_loss). Returns the dict from
    fit_leverage_to_margin — merge its size_usd/margin_usd/risk_usd/equity/
    risk_pct/leverage into the proposal before notifying, and skip the trade when
    fits is False.
    """
    from risk_manager import RiskManager

    equity    = float(snapshot.get("total_equity") or 0)
    available = snapshot.get("available_balance")
    positions = snapshot.get("positions", []) or []
    asset     = proposal.get("asset")
    entry     = proposal.get("entry")
    stop      = proposal.get("stop_loss", proposal.get("sl"))
    lev       = proposal.get("leverage") or 1
    risk_pct  = proposal.get("risk_pct")

    existing_total = sum(RiskManager._pos_margin(p) for p in positions)
    existing_asset = sum(
        RiskManager._pos_margin(p) for p in positions
        if (p.get("asset") or p.get("symbol")) == asset
    )

    return fit_leverage_to_margin(
        equity, available, risk_pct, entry, stop, lev,
        existing_total_margin=existing_total,
        existing_asset_margin=existing_asset,
    )


if __name__ == "__main__":
    import json
    import sys
    # CLI: python sizing.py equity risk_pct entry stop_loss leverage
    if len(sys.argv) != 6:
        print("Usage: python sizing.py <equity> <risk_pct> <entry> <stop_loss> <leverage>")
        sys.exit(1)
    eq, rp, en, sl, lv = map(float, sys.argv[1:6])
    print(json.dumps(compute_size(eq, rp, en, sl, lv), indent=2))
