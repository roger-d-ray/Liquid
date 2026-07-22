"""
proposal_schema.py
Normalise and validate the proposal contract shared by Claude/routine scripts.

This module is deliberately dependency-free: it gives us the useful parts of a
schema boundary (aliases, enums, clear errors) without adding a runtime package.
"""

from __future__ import annotations

from copy import deepcopy


class ProposalSchemaError(ValueError):
    """Raised when a proposal cannot be normalised into the canonical contract."""


VALID_STRATEGIES = {"range-trading", "trend-following", "momentum-trading"}
VALID_ASSETS = {"BTC", "ETH", "SOL"}
VALID_SIGNALS = {"long", "short", "no_trade"}
VALID_TIMEFRAMES = {"15m", "1h"}

STRATEGY_ALIASES = {
    "range_trading": "range-trading",
    "trend_following": "trend-following",
    "momentum_trading": "momentum-trading",
}

FIELD_ALIASES = {
    "side": "signal",
    "tp": "target",
    "take_profit": "target",
    "sl": "stop_loss",
    "stop": "stop_loss",
    "vol_ratio": "volume_ratio",
    "rr": "risk_reward",
    "rr_ratio": "risk_reward",
}

REQUIRED_FIELDS = (
    "strategy",
    "asset",
    "signal",
    "timeframe",
    "entry",
    "target",
    "stop_loss",
    "confidence",
    "price",
)

NUMERIC_FIELDS = (
    "entry",
    "target",
    "stop_loss",
    "confidence",
    "price",
    "leverage",
    "adx",
    "rsi",
    "ema9",
    "ema21",
    "ema50",
    "ema200",
    "atr",
    "volume_ratio",
    "macd_histogram",
    "support",
    "resistance",
    "target_risk_pct",
    "risk_pct",
    "minimum_risk_pct",
    "target_risk_usd",
    "risk_usd",
    "risk_retention",
    "target_size_usd",
    "size_usd",
    "margin_usd",
)


def _normalise_strategy(value) -> str:
    if value is None:
        raise ProposalSchemaError("Missing required field: strategy.")
    strategy = str(value).strip().lower()
    return STRATEGY_ALIASES.get(strategy, strategy)


def _normalise_text(value, field: str) -> str:
    if value is None:
        raise ProposalSchemaError(f"Missing required field: {field}.")
    return str(value).strip()


def _coerce_number(value, field: str):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProposalSchemaError(f"Field {field} must be numeric, got boolean.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProposalSchemaError(f"Field {field} must be numeric.") from exc


def _coerce_bool(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    return value


def normalize_proposal(raw: dict) -> dict:
    """Return the canonical proposal dict consumed by risk_manager.py.

    Accepted aliases include the older CLAUDE.md shape (`side`, `tp`, `sl`) and
    the underscore strategy names (`momentum_trading` etc.). Invalid enum values
    raise ProposalSchemaError with a user-facing reason.
    """
    if not isinstance(raw, dict):
        raise ProposalSchemaError("Proposal must be a JSON object.")

    out = deepcopy(raw)
    for old, new in FIELD_ALIASES.items():
        if new not in out and old in out:
            out[new] = out[old]

    if "price" not in out and "entry" in out:
        out["price"] = out["entry"]

    missing = [field for field in REQUIRED_FIELDS if out.get(field) is None]
    if missing:
        raise ProposalSchemaError(
            "Missing required proposal field(s): " + ", ".join(missing) + "."
        )

    out["strategy"] = _normalise_strategy(out.get("strategy"))
    out["asset"] = _normalise_text(out.get("asset"), "asset").upper()
    out["signal"] = _normalise_text(out.get("signal"), "signal").lower()
    out["timeframe"] = _normalise_text(out.get("timeframe"), "timeframe").lower()

    if out["strategy"] not in VALID_STRATEGIES:
        raise ProposalSchemaError(
            f"Invalid strategy '{out['strategy']}'. Expected one of: "
            f"{', '.join(sorted(VALID_STRATEGIES))}."
        )
    if out["asset"] not in VALID_ASSETS:
        raise ProposalSchemaError(
            f"Invalid asset '{out['asset']}'. Expected one of: "
            f"{', '.join(sorted(VALID_ASSETS))}."
        )
    if out["signal"] not in VALID_SIGNALS:
        raise ProposalSchemaError(
            f"Invalid signal '{out['signal']}'. Expected long, short, or no_trade."
        )
    if out["timeframe"] not in VALID_TIMEFRAMES:
        raise ProposalSchemaError(
            f"Invalid timeframe '{out['timeframe']}'. Intraday proposals must use 15m or 1h."
        )

    for field in NUMERIC_FIELDS:
        if field in out and out[field] is not None:
            out[field] = _coerce_number(out[field], field)

    for field in ("support_touches", "resistance_touches"):
        if field in out and out[field] is not None:
            try:
                out[field] = int(out[field])
            except (TypeError, ValueError) as exc:
                raise ProposalSchemaError(f"Field {field} must be an integer.") from exc

    for field in ("new_20d_high", "new_20d_low"):
        if field in out:
            out[field] = _coerce_bool(out[field])

    return out
