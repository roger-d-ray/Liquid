"""
manage_positions.py
Decide how to protect the PROFIT of positions that are already open — the
"intermediate take-profit" problem: a trade runs to +Xreward, then the price
reverses and the (far) take-profit never fills, so the gain evaporates.

Like intraday_exit.py this is PURE DECISION logic: it never talks to the
exchange and holds no credentials. It reads the current snapshot
(data/portfolio_state.json, written by portfolio.from_coinvest) and prints a JSON
array of protective actions the agent then executes via the Co-Invest MCP.

WHY THIS EXISTS (the +400 → -25 case)
-------------------------------------
The bot wakes once an hour. Between runs the ONLY thing protecting an open
position is the pair of resting orders placed at entry: a single take-profit and
a single stop-loss. If the take-profit is set far away it rarely fills, and a
position that was deeply in profit can round-trip back to a loss. This module
adds two protections that ratchet as the trade moves in your favour:

  1. BREAKEVEN  — once price has covered BE_AT_PROGRESS of the entry→TP distance,
                  pull the stop up to (just past) entry. Worst case becomes ~flat
                  instead of a loss.
  2. TRAIL      — once price has covered TRAIL_AT_PROGRESS, lock in
                  LOCK_FRACTION of the distance already travelled by moving the
                  stop to entry + LOCK_FRACTION*(mark-entry). Never loosens.

By default, hard profit-protection can close the whole
position when it is very close to target or the unrealised gain is large — useful
if the stop-modify tool is not pre-authorised in your policy, since a full close
uses close_positions_batch (which IS pre-authorised). It also closes time-stale
positions only when they have failed to make enough progress toward TP.

WHAT THE AGENT DOES WITH THE OUTPUT
-----------------------------------
stdout is a JSON array, one object per recommended action:
  [{"symbol":"COINVEST_RUNTIME_ID","asset":"BTC","side":"long","action":"modify_sl",
    "new_sl":68010.0,"current_sl":66800.0,"progress":0.61,"reason":"..."},
   {"symbol":"ANOTHER_RUNTIME_ID","asset":"ETH","side":"short","action":"close",
    "reason":"..."}]

  * action "modify_sl": call the Co-Invest modification tool with the exact
      runtime instrument id in `symbol`; never substitute the display `asset`.
  * action "close": call close_positions_batch(confirmed=true,
      symbols=[<symbol>]).  Already pre-authorised.
  * action "unsupported": do not mutate the account; notify and stop.

Empty array ⇒ nothing to do. Exit code is always 0 on success.

CLOUD NOTE
----------
This module needs NO cross-run state: every decision is computed from the CURRENT
snapshot (entry / mark / tp / sl / unrealised_pnl). That is deliberate — in the
cloud only logs/proposals.jsonl survives between runs (data/ is git-ignored), so
a design that depends on a local peak-PnL file would silently do nothing. A
peak-based trailing stop (give-back from the maximum favourable excursion) would
need durable state and is left as a documented v2.

All thresholds are env-overridable:
    BE_AT_PROGRESS      (default 0.5)   progress→TP at which to move SL to breakeven
    TRAIL_AT_PROGRESS   (default 0.75)  progress→TP at which to start trailing
    LOCK_FRACTION       (default 0.5)   fraction of travelled distance locked when trailing
    BE_BUFFER_PCT       (default 0.001) breakeven placed this far BEYOND entry (fees/funding)
    PROFIT_PROTECT_CLOSE(default 1)     1 to enable the hard profit-protection close
    STALE_AFTER_HOURS   (default 3)     after this many hours, require progress
    STALE_MIN_PROGRESS  (default 0.25)  minimum progress required after stale time
    MAX_HOLD_HOURS      (default 6)     late-session max hold guard
    MAX_HOLD_MIN_PROGRESS(default 0.5)  minimum progress required at max hold
    CLOSE_AT_PROGRESS   (default 0.9)   progress→TP at/above which to close (if enabled)
    CLOSE_MIN_PNL_USD   (default 0)     also close if unrealised_pnl ≥ this (0 = disabled)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import portfolio  # reuse the tolerant snapshot loader / schema


# ─── Config (env-overridable) ─────────────────────────────────────────────────

def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


BE_AT_PROGRESS        = _envf("BE_AT_PROGRESS", 0.5)
TRAIL_AT_PROGRESS     = _envf("TRAIL_AT_PROGRESS", 0.75)
LOCK_FRACTION         = _envf("LOCK_FRACTION", 0.5)
BE_BUFFER_PCT         = _envf("BE_BUFFER_PCT", 0.001)
PROFIT_PROTECT_CLOSE  = _envf("PROFIT_PROTECT_CLOSE", 1.0) >= 1.0
CLOSE_AT_PROGRESS     = _envf("CLOSE_AT_PROGRESS", 0.9)
CLOSE_MIN_PNL_USD     = _envf("CLOSE_MIN_PNL_USD", 0.0)
STALE_AFTER_HOURS     = _envf("STALE_AFTER_HOURS", 3.0)
STALE_MIN_PROGRESS    = _envf("STALE_MIN_PROGRESS", 0.25)
MAX_HOLD_HOURS        = _envf("MAX_HOLD_HOURS", 6.0)
MAX_HOLD_MIN_PROGRESS = _envf("MAX_HOLD_MIN_PROGRESS", 0.5)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(v) -> Optional[datetime]:
    """Best-effort parse of an open-time value into an aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _held_hours(pos: dict, now: Optional[datetime] = None) -> Optional[float]:
    opened = _parse_ts(_get(pos, "opened_at", "openedAt", "openTime",
                            "createdAt", "created_at", "timestamp"))
    if opened is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - opened).total_seconds() / 3600.0)


def _progress_to_target(side: str, entry: float, mark: float,
                        tp: Optional[float]) -> Optional[float]:
    """Fraction of the entry→take-profit distance already covered (0..1+).

    Returns None when it cannot be computed (missing tp, or a degenerate tp==entry).
    Negative when the trade is currently underwater.
    """
    if tp is None or entry is None or mark is None:
        return None
    span = (tp - entry) if side == "long" else (entry - tp)
    if span <= 0:
        return None
    travelled = (mark - entry) if side == "long" else (entry - mark)
    return travelled / span


def _breakeven_stop(side: str, entry: float) -> float:
    """Stop placed just PAST entry so a fill nets ~flat after fees/funding."""
    return entry * (1 + BE_BUFFER_PCT) if side == "long" else entry * (1 - BE_BUFFER_PCT)


def _trail_stop(side: str, entry: float, mark: float) -> float:
    """Lock LOCK_FRACTION of the distance travelled from entry to mark."""
    if side == "long":
        return entry + LOCK_FRACTION * (mark - entry)
    return entry - LOCK_FRACTION * (entry - mark)


def _tighter(side: str, a: float, b: float) -> float:
    """The more protective of two stop levels (higher for long, lower for short)."""
    return max(a, b) if side == "long" else min(a, b)


def _improves(side: str, new_sl: float, current_sl: Optional[float]) -> bool:
    """True if new_sl is strictly more protective than the current stop.
    Unknown current stop counts as an improvement (there is profit to lock)."""
    if current_sl is None:
        return True
    return new_sl > current_sl if side == "long" else new_sl < current_sl


def _valid_stop(side: str, new_sl: float, mark: float) -> bool:
    """A stop must sit on the losing side of the current mark, else it would
    fill instantly as a market order."""
    return new_sl < mark if side == "long" else new_sl > mark


# ─── Decision ─────────────────────────────────────────────────────────────────

def actions_for_position(pos: dict, now: Optional[datetime] = None) -> list[dict]:
    """Return the protective action(s) for a single position (usually 0 or 1)."""
    asset  = _get(pos, "asset", "displayName", default="?")
    symbol = _get(pos, "instrument_id", "instrumentId", "symbol", default=None)
    side   = str(_get(pos, "side", "signal", default="")).lower()
    entry  = _fnum(_get(pos, "entry_price", "entry", "avg_entry"))
    mark   = _fnum(_get(pos, "mark_price", "mark", "current_price"))
    tp     = _fnum(_get(pos, "tp", "take_profit", "target"))
    sl     = _fnum(_get(pos, "sl", "stop_loss", "stop"))
    size   = _fnum(_get(pos, "size_coin", "size"))
    pnl    = _fnum(_get(pos, "unrealized_pnl", "pnl", "uPnl"))
    held_h = _held_hours(pos, now)

    if side not in ("long", "short") or entry is None or mark is None:
        return []
    if not symbol:
        return [{
            "symbol": None,
            "asset": asset,
            "side": side,
            "action": "unsupported",
            "status": "unsupported",
            "reason": (
                "Co-Invest position has no execution instrument id; "
                "position mutation is blocked"
            ),
        }]

    prog = _progress_to_target(side, entry, mark, tp)

    # Close time-stale trades only when they have failed to make enough progress.
    # Missing TP counts as zero progress because the bot cannot measure the path.
    stale_progress = prog if prog is not None else 0.0
    if held_h is not None:
        if held_h >= MAX_HOLD_HOURS and stale_progress < MAX_HOLD_MIN_PROGRESS:
            return [{
                "symbol": symbol, "asset": asset, "side": side, "action": "close",
                "progress": round(prog, 3) if prog is not None else None,
                "held_hours": round(held_h, 2),
                "reason": (
                    f"Max-hold intelligente: aperta da {held_h:.1f}h ma solo "
                    f"{stale_progress*100:.0f}% verso il TP (< {MAX_HOLD_MIN_PROGRESS*100:.0f}%). "
                    "Libero margine invece di aspettare alla cieca."
                ),
            }]
        if held_h >= STALE_AFTER_HOURS and stale_progress < STALE_MIN_PROGRESS:
            return [{
                "symbol": symbol, "asset": asset, "side": side, "action": "close",
                "progress": round(prog, 3) if prog is not None else None,
                "held_hours": round(held_h, 2),
                "reason": (
                    f"Trade fermo: aperta da {held_h:.1f}h e solo "
                    f"{stale_progress*100:.0f}% verso il TP (< {STALE_MIN_PROGRESS*100:.0f}%). "
                    "Chiudo per non consumare margine."
                ),
            }]

    # Not in profit (or can't tell) → nothing to protect yet.
    in_profit = (mark > entry) if side == "long" else (mark < entry)
    if not in_profit:
        return []

    # 1. Hard profit-protection CLOSE (opt-in). Uses the pre-authorised close tool,
    #    so it works even when modify_position is not auto-confirmable.
    if PROFIT_PROTECT_CLOSE:
        by_prog = prog is not None and prog >= CLOSE_AT_PROGRESS
        by_pnl  = CLOSE_MIN_PNL_USD > 0 and pnl is not None and pnl >= CLOSE_MIN_PNL_USD
        if by_prog or by_pnl:
            why = []
            if by_prog:
                why.append(f"{prog*100:.0f}% verso il TP senza fill")
            if by_pnl:
                why.append(f"PnL +${pnl:,.0f} ≥ soglia ${CLOSE_MIN_PNL_USD:,.0f}")
            return [{
                "symbol": symbol, "asset": asset, "side": side, "action": "close",
                "progress": round(prog, 3) if prog is not None else None,
                "unrealized_pnl": pnl,
                "reason": "Profit-protection: " + " e ".join(why) + " — incasso l'intera posizione.",
            }]

    # 2. Ratchet the stop (breakeven → trail). Needs a progress reading.
    if prog is None:
        return []

    candidate: Optional[float] = None
    kind = ""
    if prog >= TRAIL_AT_PROGRESS:
        candidate = _tighter(side, _trail_stop(side, entry, mark),
                             _breakeven_stop(side, entry))
        kind = f"trailing {LOCK_FRACTION*100:.0f}% del percorso"
    elif prog >= BE_AT_PROGRESS:
        candidate = _breakeven_stop(side, entry)
        kind = "breakeven"

    if candidate is None:
        return []
    if not _valid_stop(side, candidate, mark):
        return []
    if not _improves(side, candidate, sl):
        return []

    return [{
        "symbol": symbol, "asset": asset, "side": side, "action": "modify_sl",
        "new_sl": round(candidate, 8),
        "current_sl": sl,
        "take_profit": tp,           # unchanged — pass back so the TP is preserved
        "position_size": size,       # coins, for modify_position(positionSize=...)
        "mark_price": mark,
        "progress": round(prog, 3),
        "reason": (f"{prog*100:.0f}% verso il TP → sposto lo stop a {kind} "
                   f"({candidate:.4f}), blindando il profitto."),
    }]


def actions_for_snapshot(snapshot: dict) -> list[dict]:
    positions = _get(snapshot, "positions", "open_positions", default=[]) or []
    out: list[dict] = []
    for pos in positions:
        out.extend(actions_for_position(pos))
    return out


# ─── CLI ──────────────────────────────────────────────────────────────────────

_DEMO = {
    "positions": [
        # +61% to TP, stop still below entry → move to breakeven.
        {"asset": "BTC", "symbol": "BTC-PERP", "side": "long", "size_coin": 0.1,
         "entry_price": 67000, "mark_price": 68100, "tp": 68800, "sl": 66500,
         "unrealized_pnl": 110.0},
        # +80% to TP → trail: lock half the move (well above breakeven).
        {"asset": "SOL", "symbol": "SOL-PERP", "side": "long", "size_coin": 20,
         "entry_price": 150, "mark_price": 158, "tp": 160, "sl": 147,
         "unrealized_pnl": 160.0},
        # Short, +55% to TP → breakeven.
        {"asset": "ETH", "symbol": "ETH-PERP", "side": "short", "size_coin": 1.5,
         "entry_price": 3200, "mark_price": 3090, "tp": 3000, "sl": 3260,
         "unrealized_pnl": 165.0},
        # Barely in profit (20% to TP) → do nothing yet.
        {"asset": "BTC", "symbol": "BTC-PERP", "side": "long", "size_coin": 0.05,
         "entry_price": 67000, "mark_price": 67200, "tp": 68000, "sl": 66400,
         "unrealized_pnl": 10.0},
    ]
}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if "--demo" in sys.argv:
        print(json.dumps(actions_for_snapshot(_DEMO), ensure_ascii=False, indent=2))
        return 0

    try:
        snapshot = portfolio.load_portfolio_state()
    except portfolio.PortfolioUnavailable as e:
        print(f"[manage_positions] snapshot non disponibile: {e}", file=sys.stderr)
        print("[]")
        return 0

    print(json.dumps(actions_for_snapshot(snapshot), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
