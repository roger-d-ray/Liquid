"""
intraday_exit.py
Decide which open positions must be flattened to honour the intraday-only policy
(no overnight holds). This is PURE DECISION logic — it never talks to the
exchange and holds no credentials.

WHO TRIGGERS THE FLATTEN (100% automatic)
-----------------------------------------
The 60-min cloud routine is the trigger. That hourly cron IS what makes this
automatic: on EVERY run the agent calls get_portfolio() (Co-Invest MCP), persists
the snapshot (portfolio.save_portfolio_state), then runs this script. The script
prints the positions that must close; the agent closes each via the MCP
close_position()/close_positions_batch(). No daemon, no human — as long as the
routine keeps firing hourly, the flatten happens on its own.

TWO INDEPENDENT RULES (a position matching EITHER is flattened)
--------------------------------------------------------------
1. End-of-day flatten (HARD GUARANTEE): at/after FLATTEN_HOUR_UTC every open
   position is closed, so nothing is ever carried overnight. This rule needs NO
   per-position timestamp, so it works even though the MCP snapshot may not carry
   an open time. This is the backstop that makes "closes within the day" true.
2. Max-hold (BEST EFFORT): if the position's open time is known (opened_at in the
   snapshot), close it once it has been open longer than MAX_HOLD_HOURS, so scalps
   don't linger even mid-session. Silently inactive when the open time is absent.

Both thresholds are overridable via environment variables so the policy can be
tuned without editing code:
    FLATTEN_HOUR_UTC   (default 23)   hour-of-day (UTC) at/after which all close
    MAX_HOLD_HOURS     (default 6)    max holding time when opened_at is known

Output (CLI): a JSON array on stdout, one object per position to close:
    [{"symbol": "BTC-PERP", "asset": "BTC", "side": "long", "reason": "..."}]
`symbol` is the perp id to pass to close_positions_batch(symbols=[...]); `asset`
is the display name for the Telegram message. An empty array means nothing to
flatten. Exit code is always 0 on success so the routine can parse stdout
unconditionally.

NOTE on how the agent closes: the only close tool the agent may call is the
Co-Invest MCP close_positions_batch(confirmed=true, symbols=[...]). The singular
close_position tool is SYSTEM INTERNAL and must never be called by the model.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import portfolio  # reuse the tolerant snapshot loader / schema

# ─── Config (env-overridable) ─────────────────────────────────────────────────

FLATTEN_HOUR_UTC = int(os.environ.get("FLATTEN_HOUR_UTC", "23"))
MAX_HOLD_HOURS   = float(os.environ.get("MAX_HOLD_HOURS", "6"))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _get(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _parse_ts(v) -> Optional[datetime]:
    """Best-effort parse of an open-time value into an aware UTC datetime.
    Accepts epoch seconds, epoch milliseconds, or an ISO-8601 string."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e12:          # milliseconds
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


# ─── Decision ─────────────────────────────────────────────────────────────────

def is_flatten_window(now: Optional[datetime] = None) -> bool:
    """True during the end-of-day flatten window (UTC hour >= FLATTEN_HOUR_UTC)."""
    return _now(now).hour >= FLATTEN_HOUR_UTC


def positions_to_flatten(snapshot: dict, now: Optional[datetime] = None) -> list[dict]:
    """Return the positions that must be closed now, each annotated with a reason.

    `snapshot` is a portfolio_state.json dict (as written by
    portfolio.from_coinvest). Tolerant of key-name variants (side/signal,
    asset/symbol, opened_at/openedAt/...).
    """
    now = _now(now)
    positions = _get(snapshot, "positions", "open_positions", default=[]) or []
    eod = is_flatten_window(now)

    out: list[dict] = []
    for pos in positions:
        asset  = _get(pos, "asset", "displayName", default="?")
        symbol = _get(pos, "symbol", "asset", default=asset)  # perp id for closing
        side   = str(_get(pos, "side", "signal", default="?")).lower()
        reason: Optional[str] = None

        if eod:
            reason = (
                f"end-of-day flatten (UTC hour {now.hour:02d} >= "
                f"{FLATTEN_HOUR_UTC:02d}) — no overnight holds"
            )
        else:
            opened = _parse_ts(
                _get(pos, "opened_at", "openedAt", "openTime", "createdAt", "timestamp")
            )
            if opened is not None:
                held_h = (now - opened).total_seconds() / 3600.0
                if held_h >= MAX_HOLD_HOURS:
                    reason = (
                        f"max hold exceeded ({held_h:.1f}h >= {MAX_HOLD_HOURS:g}h)"
                    )

        if reason:
            out.append({"symbol": symbol, "asset": asset, "side": side, "reason": reason})

    return out


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        snapshot = portfolio.load_portfolio_state()
    except portfolio.PortfolioUnavailable as e:
        # No snapshot yet → nothing to flatten. Report on stderr, empty list on stdout.
        print(f"[intraday_exit] snapshot non disponibile: {e}", file=sys.stderr)
        print("[]")
        return 0

    plan = positions_to_flatten(snapshot)
    print(json.dumps(plan, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
