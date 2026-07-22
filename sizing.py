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

from execution_instrument import DEFAULT_MINIMUM_COLLATERAL_USD


# A margin-constrained trade may be reduced, but it must retain at least 75% of
# the intended stop risk and may never risk less than 0.75% of account equity.
MIN_RISK_RETENTION = 0.75
MIN_ABSOLUTE_RISK_PCT = 0.0075


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
    lev      = float(leverage) if leverage is not None else 1.0

    if not all(math.isfinite(value) for value in (equity, risk_pct, entry, stop, lev)):
        raise ValueError("gli input di sizing devono essere numeri finiti")

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
        raise ValueError("leverage deve essere > 0")

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


def _apply_emitted_collateral_floor(out: dict, minimum_collateral_usd: float) -> None:
    """Make the returned size/leverage tuple satisfy the floor after rounding."""
    size = float(out["size_usd"])
    leverage = float(out["leverage"])
    if not all(math.isfinite(value) and value > 0 for value in (size, leverage)):
        raise ValueError("size_usd e leverage emessi devono essere finiti e positivi")
    safe_leverage = size / minimum_collateral_usd
    if leverage > safe_leverage:
        # The exact tuple sent to build_execution_order must remain on the safe
        # side of the floor after size_usd is rounded for execution.
        leverage = math.nextafter(safe_leverage, 0.0)
        out["leverage"] = leverage
    out["margin_usd"] = round(size / leverage, 2)


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
                           max_per_asset_margin_pct: float = None,
                           minimum_collateral_usd: float =
                           DEFAULT_MINIMUM_COLLATERAL_USD,
                           minimum_notional_usd: float = None) -> dict:
    """Size a trade and adapt leverage within the execution constraints (up to the
    cap) before giving up — then discard if it still doesn't fit.

    The ideal notional is fixed by risk-based sizing (independent of leverage), so
    margin = notional / leverage: raising leverage lowers margin without changing
    the € risk. We compute the minimum leverage that fits inside the tightest of
    three budgets (per-asset cap, total cap, available balance), bounded by the
    liquidation-safe ceiling.

    When the ideal position does not fit even at the leverage ceiling, the
    notional is clipped to the available margin budget. The smaller trade is
    accepted only if its actual stop risk retains at least
    MIN_RISK_RETENTION of the target risk and is at least
    MIN_ABSOLUTE_RISK_PCT of equity.

    Returns the compute_size() dict plus: fits (bool), leverage (final),
    requested_leverage, adjusted (bool), notional, margin_budget, target/actual
    risk metadata, and a human `reason`. Caps default to risk_manager's (single
    source of truth).
    """
    from risk_manager import (MAX_LEVERAGE, MAX_TOTAL_MARGIN_PCT,
                              MAX_PER_ASSET_MARGIN_PCT)
    max_leverage             = MAX_LEVERAGE if max_leverage is None else max_leverage
    max_total_margin_pct     = MAX_TOTAL_MARGIN_PCT if max_total_margin_pct is None else max_total_margin_pct
    max_per_asset_margin_pct = MAX_PER_ASSET_MARGIN_PCT if max_per_asset_margin_pct is None else max_per_asset_margin_pct

    equity     = float(equity)
    risk_pct   = float(risk_pct)
    entry      = float(entry)
    stop_loss  = float(stop_loss)
    leverage   = float(leverage)
    configured_collateral = float(minimum_collateral_usd)
    if not math.isfinite(configured_collateral) or configured_collateral <= 0:
        raise ValueError("minimum_collateral_usd deve essere finito e positivo")
    minimum_collateral_usd = max(
        DEFAULT_MINIMUM_COLLATERAL_USD,
        configured_collateral,
    )
    if minimum_notional_usd is not None:
        minimum_notional_usd = float(minimum_notional_usd)
        if not math.isfinite(minimum_notional_usd) or minimum_notional_usd <= 0:
            raise ValueError("minimum_notional_usd deve essere finito e positivo")

    base       = compute_size(equity, risk_pct, entry, stop_loss, leverage)
    # Keep full precision for acceptance decisions; round only returned values.
    stop_dist  = abs(entry - stop_loss)
    notional   = risk_pct * equity / stop_dist * entry
    stop_pct   = base["stop_dist_pct"]
    target_risk_usd = risk_pct * equity
    minimum_risk_pct = max(
        risk_pct * MIN_RISK_RETENTION,
        MIN_ABSOLUTE_RISK_PCT,
    )

    existing_total_margin = float(existing_total_margin or 0.0)
    existing_asset_margin = float(existing_asset_margin or 0.0)

    budget_total = max_total_margin_pct * equity - existing_total_margin
    budget_asset = max_per_asset_margin_pct * equity - existing_asset_margin
    budget_avail = float(available_balance) if available_balance is not None else budget_total
    budget       = min(budget_total, budget_asset, budget_avail)

    risk_lev_ceiling = _liq_safe_leverage(stop_pct, max_leverage)
    collateral_lev_ceiling = notional / minimum_collateral_usd
    lev_ceiling = min(risk_lev_ceiling, collateral_lev_ceiling)

    out = {
        **base,
        "requested_leverage": float(leverage),
        "notional":           round(notional, 2),
        "target_size_usd":    round(notional, 2),
        "target_risk_pct":    round(risk_pct, 4),
        "target_risk_usd":    round(target_risk_usd, 2),
        "minimum_risk_pct":   round(minimum_risk_pct, 4),
        "risk_retention":     1.0,
        "size_adjusted":      False,
        "leverage_adjusted":  False,
        "margin_budget":      round(budget, 2),
        "leverage_ceiling":   round(lev_ceiling, 2),
        "minimum_collateral_usd": round(minimum_collateral_usd, 2),
        "minimum_notional_usd": minimum_notional_usd,
    }

    if budget <= 0:
        out.update(fits=False, adjusted=False,
                   reason=("Cap di margine già saturi dalle posizioni aperte: "
                           "nessun budget per un nuovo trade."))
        return out

    if budget + 1e-12 < minimum_collateral_usd:
        out.update(
            fits=False,
            adjusted=False,
            reason=(
                f"Trade scartato: budget margine ${budget:,.2f} sotto il "
                f"collaterale minimo Co-Invest ${minimum_collateral_usd:,.2f}."
            ),
        )
        return out

    if notional + 1e-12 < minimum_collateral_usd:
        out.update(
            fits=False,
            adjusted=False,
            reason=(
                f"Trade scartato: nozionale ${notional:,.2f} insufficiente "
                f"anche a 1x per il collaterale minimo Co-Invest "
                f"${minimum_collateral_usd:,.2f}."
            ),
        )
        return out

    if minimum_notional_usd is not None and notional + 1e-12 < minimum_notional_usd:
        out.update(
            fits=False,
            adjusted=False,
            reason=(
                f"Trade scartato: nozionale ${notional:,.2f} sotto il minimo "
                f"strumento ${minimum_notional_usd:,.2f}."
            ),
        )
        return out

    lev_needed = notional / budget
    requested_within_ceiling = min(float(leverage), lev_ceiling)

    if lev_needed > lev_ceiling + 1e-9:
        max_notional = budget * lev_ceiling
        actual_risk_usd = max_notional * stop_dist / entry
        actual_risk_pct = actual_risk_usd / equity
        risk_retention = actual_risk_pct / risk_pct
        clipped = compute_size(
            equity, actual_risk_pct, entry, stop_loss, lev_ceiling
        )
        leverage_adjusted = abs(lev_ceiling - leverage) > 1e-9

        out.update(clipped)
        out.update(
            leverage=float(lev_ceiling),
            requested_leverage=float(leverage),
            notional=round(max_notional, 2),
            target_size_usd=round(notional, 2),
            target_risk_pct=round(risk_pct, 4),
            target_risk_usd=round(target_risk_usd, 2),
            minimum_risk_pct=round(minimum_risk_pct, 4),
            risk_retention=round(risk_retention, 4),
            size_adjusted=True,
            leverage_adjusted=leverage_adjusted,
            margin_budget=round(budget, 2),
            leverage_ceiling=round(lev_ceiling, 2),
        )
        _apply_emitted_collateral_floor(out, minimum_collateral_usd)
        leverage_adjusted = abs(float(out["leverage"]) - leverage) > 1e-9
        out["leverage_adjusted"] = leverage_adjusted

        if (
            minimum_notional_usd is not None
            and float(out["size_usd"]) + 1e-12 < minimum_notional_usd
        ):
            out.update(
                fits=False,
                adjusted=False,
                reason=(
                    f"Trade scartato: size eseguibile ${out['size_usd']:,.2f} "
                    f"sotto il minimo strumento ${minimum_notional_usd:,.2f}."
                ),
            )
            return out

        if actual_risk_pct + 1e-12 >= minimum_risk_pct:
            out.update(
                fits=True,
                adjusted=True,
                reason=(
                    f"Size ridotta per rientrare nel margine: rischio effettivo "
                    f"{actual_risk_pct*100:.2f}% (retention "
                    f"{risk_retention*100:.1f}%) ≥ minimo "
                    f"{minimum_risk_pct*100:.2f}%."
                ),
            )
        else:
            out.update(
                fits=False,
                adjusted=False,
                reason=(
                    f"Trade scartato: la size massima consentita rischierebbe "
                    f"{actual_risk_pct*100:.2f}% dell'equity, sotto il minimo "
                    f"{minimum_risk_pct*100:.2f}% (retention "
                    f"{risk_retention*100:.1f}%, richiesto almeno "
                    f"{MIN_RISK_RETENTION*100:.0f}%)."
                ),
            )
        return out

    final_lev = math.ceil(max(requested_within_ceiling, lev_needed))
    if final_lev > lev_ceiling:
        # A fractional ceiling is valid in Co-Invest and avoids crossing the
        # minimum-collateral boundary merely because of integer rounding.
        final_lev = lev_ceiling

    adjusted = abs(final_lev - float(leverage)) > 1e-9
    out.update(compute_size(equity, risk_pct, entry, stop_loss, final_lev))
    out.update(fits=True, adjusted=adjusted, leverage=float(final_lev),
               leverage_adjusted=adjusted, size_adjusted=False,
               requested_leverage=float(leverage),
               notional=round(notional, 2), margin_budget=round(budget, 2),
               leverage_ceiling=round(lev_ceiling, 2))
    _apply_emitted_collateral_floor(out, minimum_collateral_usd)
    final_lev = float(out["leverage"])
    adjusted = abs(final_lev - float(leverage)) > 1e-9
    out["adjusted"] = adjusted
    out["leverage_adjusted"] = adjusted
    if (
        minimum_notional_usd is not None
        and float(out["size_usd"]) + 1e-12 < minimum_notional_usd
    ):
        out.update(
            fits=False,
            adjusted=False,
            reason=(
                f"Trade scartato: size eseguibile ${out['size_usd']:,.2f} "
                f"sotto il minimo strumento ${minimum_notional_usd:,.2f}."
            ),
        )
        return out
    if final_lev > leverage + 1e-9:
        out["reason"] = (
            f"Leva alzata da {leverage:g}x a {final_lev:g}x per rientrare nel "
            f"margine (margine ${out['margin_usd']:,.0f} <= budget "
            f"${budget:,.0f})."
        )
    elif final_lev < leverage - 1e-9:
        out["reason"] = (
            f"Leva ridotta da {leverage:g}x a {final_lev:g}x per mantenere "
            f"almeno ${minimum_collateral_usd:,.2f} di collaterale."
        )
    else:
        out["reason"] = f"Rientra nel margine alla leva richiesta ({leverage:g}x)."
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
    from execution_instrument import assert_executable_proposal
    from risk_manager import MAX_LEVERAGE, RiskManager

    assert_executable_proposal(proposal)

    equity    = float(snapshot.get("total_equity") or 0)
    available = snapshot.get("available_balance")
    positions = snapshot.get("positions", []) or []
    asset     = proposal.get("asset")
    instrument = proposal["market_context"]["instrument"]
    connector_max_leverage = float(instrument["maximum_leverage"])
    maximum_leverage = min(MAX_LEVERAGE, connector_max_leverage)
    minimum_collateral = max(
        DEFAULT_MINIMUM_COLLATERAL_USD,
        float(proposal["market_context"]["minimum_collateral_usd"]),
    )
    minimum_notional = instrument.get("minimum_notional")
    proposal_ids = {
        str(value).strip().casefold()
        for value in (asset, instrument.get("base_asset"), instrument.get("instrument_id"))
        if value
    }
    entry     = proposal["market_context"]["execution_price"]
    stop      = proposal.get("stop_loss", proposal.get("sl"))
    lev       = 1 if proposal.get("leverage") is None else proposal.get("leverage")
    risk_pct  = proposal.get("risk_pct")

    existing_total = sum(RiskManager._pos_margin(p) for p in positions)
    existing_asset = sum(
        RiskManager._pos_margin(p) for p in positions
        if any(
            str(value).strip().casefold() in proposal_ids
            for value in (
                p.get("asset"), p.get("base_asset"), p.get("symbol"),
                p.get("instrument_id"),
            ) if value
        )
    )

    return fit_leverage_to_margin(
        equity, available, risk_pct, entry, stop, lev,
        existing_total_margin=existing_total,
        existing_asset_margin=existing_asset,
        max_leverage=maximum_leverage,
        minimum_collateral_usd=minimum_collateral,
        minimum_notional_usd=minimum_notional,
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
