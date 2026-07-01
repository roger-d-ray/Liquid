"""
portfolio.py
Read-only helpers to load and format the account portfolio for Telegram.

Data source (Opzione B): the Co-Invest MCP assistant (during the 60-min routine,
STEP 5) calls get_portfolio() and persists the result to
data/portfolio_state.json. This module only READS that cached file and turns it
into a readable Telegram message. It never places, modifies, or closes orders —
the trading flow (STEP 0-6) is untouched.

The reader is intentionally tolerant of field-name variants because get_portfolio()
and risk_manager use slightly different key names for the same concept
(e.g. total_equity vs equity, signal vs side, notional vs size_usd).

No secrets live here: this file reads a local JSON snapshot only.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# risk_manager.py already reads/writes this same path — keep them in sync.
PORTFOLIO_PATH = Path(__file__).parent / "data" / "portfolio_state.json"


class PortfolioUnavailable(Exception):
    """Raised when the cached portfolio snapshot is missing or unreadable."""


# ─── Tolerant field access ────────────────────────────────────────────────────

def _first(d: dict, *keys, default=None):
    """Return the first present, non-None value among the given keys."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _fnum(v):
    """Best-effort float; returns None if not convertible."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_money(v, decimals=2):
    n = _fnum(v)
    if n is None:
        return "—"
    return f"${n:,.{decimals}f}"


def _fmt_price(v):
    n = _fnum(v)
    if n is None:
        return "—"
    # More decimals for sub-dollar assets, fewer for large prices.
    if abs(n) >= 1000:
        return f"${n:,.1f}"
    if abs(n) >= 1:
        return f"${n:,.2f}"
    return f"${n:,.4f}"


def _fmt_signed(v, prefix="$"):
    n = _fnum(v)
    if n is None:
        return "—"
    sign = "+" if n >= 0 else "-"
    return f"{sign}{prefix}{abs(n):,.2f}"


# ─── Load ─────────────────────────────────────────────────────────────────────

def load_portfolio_state(path: Path = PORTFOLIO_PATH) -> dict:
    """
    Load the cached portfolio snapshot from disk.

    Raises PortfolioUnavailable with a human-readable reason if the file is
    missing or contains invalid JSON, so callers can surface the detail to the
    user instead of crashing.
    """
    if not path.exists():
        raise PortfolioUnavailable(
            f"snapshot non trovato ({path.name}). "
            "L'agente non ha ancora scritto lo stato del portafoglio "
            "(get_portfolio via Co-Invest MCP)."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PortfolioUnavailable(f"impossibile leggere {path.name}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise PortfolioUnavailable(f"{path.name} non è JSON valido: {e}") from e


# ─── Format ───────────────────────────────────────────────────────────────────

def format_portfolio(data: dict) -> str:
    """
    Build a readable Telegram message (Markdown) from a portfolio snapshot.

    Recognised top-level keys (any variant): total_equity/equity,
    available_balance/available/free, margin_used/used_margin/margin,
    updated_at/timestamp/ts.
    Each position (positions/open_positions) may use: asset/symbol,
    side/signal, size_usd/notional/notional_usd/size, entry_price/entry,
    mark_price/mark/current_price, unrealized_pnl/pnl/uPnl, leverage/lev.
    """
    equity     = _first(data, "total_equity", "equity", "account_value")
    available  = _first(data, "available_balance", "available", "free", "cash")
    margin     = _first(data, "margin_used", "used_margin", "margin")
    updated    = _first(data, "updated_at", "timestamp", "ts")
    positions  = _first(data, "positions", "open_positions", default=[]) or []

    lines = ["💼 *Portafoglio Liquid*", ""]
    lines.append(f"*Equity:*        {_fmt_money(equity)}")
    lines.append(f"*Disponibile:*   {_fmt_money(available)}")
    lines.append(f"*Margine usato:* {_fmt_money(margin)}")
    lines.append(f"*Posizioni:*     {len(positions)}")

    if positions:
        lines.append("")
        lines.append("*Posizioni aperte:*")
        for pos in positions:
            asset   = _first(pos, "asset", "symbol", default="?")
            side    = str(_first(pos, "side", "signal", default="?")).upper()
            size    = _first(pos, "size_usd", "notional", "notional_usd", "size")
            entry   = _first(pos, "entry_price", "entry", "avg_entry")
            mark    = _first(pos, "mark_price", "mark", "current_price")
            pnl     = _first(pos, "unrealized_pnl", "pnl", "uPnl", "unrealizedPnl")
            lev     = _first(pos, "leverage", "lev")

            side_emoji = "🟢" if side in ("LONG", "BUY") else "🔴"
            lev_str = f"{_fnum(lev):g}x" if _fnum(lev) is not None else "—"

            lines.append("")
            lines.append(f"{side_emoji} *{asset}* {side} · {lev_str} · {_fmt_money(size)}")
            detail = f"   Entry {_fmt_price(entry)}"
            if _fnum(mark) is not None:
                detail += f" · Mark {_fmt_price(mark)}"
            lines.append(detail)
            if _fnum(pnl) is not None:
                lines.append(f"   PnL {_fmt_signed(pnl)}")

    # Freshness footer.
    if updated:
        lines += ["", f"_Aggiornato: {updated}_"]
    lines += ["", "_Sola lettura — nessun ordine eseguito._"]
    return "\n".join(lines)


def build_portfolio_message(path: Path = PORTFOLIO_PATH) -> str:
    """load + format in one call. Propagates PortfolioUnavailable."""
    return format_portfolio(load_portfolio_state(path))


# ─── Co-Invest MCP bridge (Opzione B) ─────────────────────────────────────────

def from_coinvest(gp: dict) -> dict:
    """
    Map a Co-Invest MCP get_portfolio() payload into the portfolio_state.json
    schema this module reads.

    The MCP uses different field names/units than our snapshot:
      account.equity/available_balance/margin_used  → top-level
      position.symbol="BTC-PERP" / displayName="BTC" → asset (prefer displayName)
      position.size is in COIN units → we also compute size_usd = |size| * markPx
      entryPx → entry_price, markPx → mark_price, unrealizedPnl → unrealized_pnl
    """
    acct = gp.get("account", {}) or {}
    out = {
        "total_equity":      _fnum(_first(acct, "equity", "account_value")),
        "available_balance": _fnum(acct.get("available_balance")),
        "margin_used":       _fnum(acct.get("margin_used")),
        "updated_at":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "positions":         [],
    }
    for p in gp.get("positions", []) or []:
        size = _fnum(p.get("size"))
        mark = _fnum(p.get("markPx"))
        notional = abs(size * mark) if (size is not None and mark is not None) else None
        out["positions"].append({
            "asset":          p.get("displayName") or p.get("symbol"),
            # Raw perp symbol (e.g. "BTC-PERP") — required by
            # close_positions_batch(symbols=[...]); close needs the perp id, not
            # the display name.
            "symbol":         p.get("symbol"),
            "side":           p.get("side"),
            "size_coin":      size,
            "size_usd":       notional,
            "entry_price":    _fnum(p.get("entryPx")),
            "mark_price":     mark,
            "unrealized_pnl": _fnum(p.get("unrealizedPnl")),
            "leverage":       _fnum(p.get("leverage")),
            "tp":             _fnum(p.get("tp")),
            "sl":             _fnum(p.get("sl")),
            "liquidation_px": _fnum(p.get("liquidationPx")),
            # Best-effort open time — used by intraday_exit.py for the max-hold
            # flatten rule. Absent in some MCP payloads → left None (the
            # end-of-day flatten rule guarantees no overnight hold regardless).
            "opened_at":      _first(p, "openedAt", "openTime", "createdAt",
                                     "created_at", "timestamp"),
        })
    return out


def save_portfolio_state(data: dict, path: Path = PORTFOLIO_PATH) -> Path:
    """Persist a snapshot to disk (used by the agent during the 60-min routine)."""
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# ─── CLI (local preview / test fixture) ───────────────────────────────────────

_SAMPLE = {
    "total_equity": 10_000.0,
    "available_balance": 7_350.0,
    "margin_used": 2_650.0,
    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "positions": [
        {
            "asset": "BTC", "side": "long", "size_usd": 2000.0,
            "entry_price": 67_420.0, "mark_price": 68_100.0,
            "unrealized_pnl": 20.18, "leverage": 2,
        },
        {
            "asset": "ETH", "side": "short", "notional": 650.0,
            "entry_price": 3_180.0, "mark_price": 3_150.0,
            "unrealized_pnl": 6.13, "leverage": 3,
        },
    ],
}

if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if "--sample-write" in sys.argv:
        PORTFOLIO_PATH.parent.mkdir(exist_ok=True)
        PORTFOLIO_PATH.write_text(json.dumps(_SAMPLE, indent=2), encoding="utf-8")
        print(f"Sample scritto in {PORTFOLIO_PATH}")
        sys.exit(0)

    if "--sample" in sys.argv:
        print(format_portfolio(_SAMPLE))
        sys.exit(0)

    try:
        print(build_portfolio_message())
    except PortfolioUnavailable as e:
        print(f"[portfolio] non disponibile: {e}")
        sys.exit(1)
