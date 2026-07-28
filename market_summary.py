"""Build a compact, stable analysis view of ``data/market_data.json``.

The fetcher output is intentionally rich and separates closed bars from
indicators. This command gives the scheduled routine one documented JSON
shape, so it never needs ad-hoc Python to discover that schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from data_fetcher import (
    ASSETS,
    MARKET_DATA_SCHEMA_VERSION,
    REQUIRED_SIGNAL_TIMEFRAMES,
    validate_signal_ready_output,
)
from market_bars import utc_ms


DEFAULT_INPUT_PATH = Path(__file__).parent / "data" / "market_data.json"
CONTEXT_TIMEFRAMES = ("4h", "1d")
DEFAULT_RECENT_BARS = 8
DEFAULT_CONTEXT_BARS = 3

LIVE_FIELDS = (
    "price",
    "price_timestamp",
    "change_since_utc_midnight_pct",
    "rolling_24h_change_pct",
    "base_volume",
    "quote_volume",
    "estimated_quote_volume",
    "high_24h",
    "low_24h",
    "source",
    "aggregation_method",
    "volume_window",
    "completeness",
    "quality_flags",
    "rolling_24h_reference_rule",
    "rolling_24h_reference_timestamp",
    "rolling_24h_actual_interval_seconds",
    "rolling_24h_reference_distance_seconds",
    "funding_rate",
    "oi",
    "long_pct",
    "short_pct",
    "derivatives_venue",
    "signal_venue",
)


class MarketSummaryError(ValueError):
    """Raised when market data cannot be represented safely for analysis."""


def _object(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise MarketSummaryError(f"{path} deve essere un oggetto JSON")
    return value


def _list(value, path: str) -> list:
    if not isinstance(value, list):
        raise MarketSummaryError(f"{path} deve essere un array JSON")
    return value


def _analysis_time(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MarketSummaryError("timestamp mancante o non valido")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketSummaryError(f"timestamp non ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise MarketSummaryError("timestamp senza timezone")
    return parsed


def _validate_layout(data: dict) -> datetime:
    data = _object(data, "root")
    actual_version = data.get("schema_version")
    if actual_version != MARKET_DATA_SCHEMA_VERSION:
        raise MarketSummaryError(
            "schema market_data non supportato: "
            f"atteso {MARKET_DATA_SCHEMA_VERSION}, ricevuto {actual_version!r}"
        )

    analysis_time = _analysis_time(data.get("timestamp"))
    assets = _object(data.get("assets"), "assets")
    for asset in ASSETS:
        asset_data = _object(assets.get(asset), f"assets.{asset}")
        _object(asset_data.get("live"), f"assets.{asset}.live")
        timeframes = _object(
            asset_data.get("timeframes"), f"assets.{asset}.timeframes"
        )
        indicators = _object(
            asset_data.get("indicators"), f"assets.{asset}.indicators"
        )
        for timeframe in REQUIRED_SIGNAL_TIMEFRAMES:
            bars = _list(
                timeframes.get(timeframe),
                f"assets.{asset}.timeframes.{timeframe}",
            )
            for index, bar in enumerate(bars):
                _object(
                    bar,
                    f"assets.{asset}.timeframes.{timeframe}[{index}]",
                )
            _object(
                indicators.get(timeframe),
                f"assets.{asset}.indicators.{timeframe}",
            )

    try:
        validate_signal_ready_output(data)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise MarketSummaryError(str(exc)) from exc
    return analysis_time


def _nested(mapping: dict, parent: str, child: str):
    value = mapping.get(parent)
    return value.get(child) if isinstance(value, dict) else None


def _indicator_snapshot(indicators: dict) -> dict:
    """Expose the analysis fields under stable, proposal-friendly names."""
    return {
        "ema9": indicators.get("ema9"),
        "ema21": indicators.get("ema21"),
        "ema50": indicators.get("ema50"),
        "ema200": indicators.get("ema200"),
        "rsi": indicators.get("rsi"),
        "atr": indicators.get("atr"),
        "adx": indicators.get("adx"),
        "plus_di": indicators.get("plus_di"),
        "minus_di": indicators.get("minus_di"),
        "volume_sma20": indicators.get("vol_sma20"),
        "volume_ratio": indicators.get("vol_ratio"),
        "macd_line": _nested(indicators, "macd", "macd"),
        "macd_signal": _nested(indicators, "macd", "signal"),
        "macd_histogram": _nested(indicators, "macd", "histogram"),
        "bollinger_upper": _nested(indicators, "bollinger", "upper"),
        "bollinger_middle": _nested(indicators, "bollinger", "middle"),
        "bollinger_lower": _nested(indicators, "bollinger", "lower"),
        "stochastic_k": _nested(indicators, "stochastic", "k"),
        "stochastic_d": _nested(indicators, "stochastic", "d"),
        "support": indicators.get("support"),
        "resistance": indicators.get("resistance"),
        "swing_highs": indicators.get("swing_highs"),
        "swing_lows": indicators.get("swing_lows"),
        "high_20_bars": indicators.get("high_20_bars"),
        "low_20_bars": indicators.get("low_20_bars"),
        "high_55_bars": indicators.get("high_55_bars"),
        "low_55_bars": indicators.get("low_55_bars"),
        "high_20_days": indicators.get("high_20_days"),
        "low_20_days": indicators.get("low_20_days"),
    }


def _timeframe_summary(bars: list, indicators: dict, recent_bars: int) -> dict:
    return {
        "closed_bar_count": len(bars),
        "recent_closed_bars": bars[-recent_bars:],
        "indicators": _indicator_snapshot(indicators),
    }


def _context_summary(
    asset: str,
    asset_data: dict,
    timeframe: str,
    analysis_time: datetime,
    recent_bars: int,
    warnings: list[str],
) -> dict | None:
    timeframes = asset_data["timeframes"]
    indicators_by_timeframe = asset_data["indicators"]
    bars = timeframes.get(timeframe)
    indicators = indicators_by_timeframe.get(timeframe)
    if not isinstance(bars, list) or not bars:
        warnings.append(f"{asset} {timeframe}: contesto barre non disponibile")
        return None
    if not isinstance(indicators, dict) or not indicators:
        warnings.append(f"{asset} {timeframe}: contesto indicatori non disponibile")
        return None

    analysis_time_ms = utc_ms(analysis_time)
    usable_bars = [
        bar
        for bar in bars
        if isinstance(bar, dict)
        and bar.get("is_final") is True
        and bar.get("completeness", "complete") == "complete"
        and isinstance(bar.get("available_at"), (int, float))
        and int(bar["available_at"]) <= analysis_time_ms
    ]
    if len(usable_bars) != len(bars):
        warnings.append(
            f"{asset} {timeframe}: escluse {len(bars) - len(usable_bars)} "
            "barre di contesto non finali/non disponibili"
        )
    if not usable_bars:
        return None
    return _timeframe_summary(usable_bars, indicators, recent_bars)


def build_market_summary(
    data: dict,
    *,
    recent_bars: int = DEFAULT_RECENT_BARS,
    context_bars: int = DEFAULT_CONTEXT_BARS,
) -> dict:
    """Return the stable JSON view consumed by the analysis routine."""
    if recent_bars <= 0 or context_bars <= 0:
        raise MarketSummaryError("recent_bars e context_bars devono essere positivi")

    analysis_time = _validate_layout(data)
    summary = {
        "schema_version": MARKET_DATA_SCHEMA_VERSION,
        "analysis_timestamp": data["timestamp"],
        "signal_timeframes": list(REQUIRED_SIGNAL_TIMEFRAMES),
        "context_timeframes": list(CONTEXT_TIMEFRAMES),
        "assets": {},
    }

    for asset in ASSETS:
        asset_data = data["assets"][asset]
        live = asset_data["live"]
        warnings: list[str] = []
        asset_summary = {
            "live": {field: live.get(field) for field in LIVE_FIELDS},
            "signal_timeframes": {},
            "context_timeframes": {},
            "warnings": warnings,
        }

        for timeframe in REQUIRED_SIGNAL_TIMEFRAMES:
            asset_summary["signal_timeframes"][timeframe] = _timeframe_summary(
                asset_data["timeframes"][timeframe],
                asset_data["indicators"][timeframe],
                recent_bars,
            )

        for timeframe in CONTEXT_TIMEFRAMES:
            context = _context_summary(
                asset,
                asset_data,
                timeframe,
                analysis_time,
                context_bars,
                warnings,
            )
            if context is not None:
                asset_summary["context_timeframes"][timeframe] = context

        summary["assets"][asset] = asset_summary

    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Produce il riepilogo JSON stabile per l'analisi Liquid"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT_PATH,
        help="market_data.json da leggere",
    )
    parser.add_argument(
        "--recent-bars",
        type=int,
        default=DEFAULT_RECENT_BARS,
        help="numero di ClosedBar recenti per 15m/1h",
    )
    parser.add_argument(
        "--context-bars",
        type=int,
        default=DEFAULT_CONTEXT_BARS,
        help="numero di ClosedBar recenti per 4h/1d",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        summary = build_market_summary(
            raw,
            recent_bars=args.recent_bars,
            context_bars=args.context_bars,
        )
    except (OSError, json.JSONDecodeError, MarketSummaryError) as exc:
        print(f"ERROR: market summary non disponibile: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
