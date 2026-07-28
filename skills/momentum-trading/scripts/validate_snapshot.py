#!/usr/bin/env python3
"""Validate a normalized crypto momentum market snapshot.

The validator checks causality, candle structure, freshness, and live-mode
requirements. It does not calculate indicators or authorize trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


LIVE_RISK_FIELDS = (
    "risk_fraction_per_trade",
    "max_total_open_risk_fraction",
    "max_daily_loss_fraction",
    "max_drawdown_fraction",
    "max_leverage",
    "max_spread_bps",
    "max_slippage_bps",
)

CANDLE_FIELDS = (
    "open_time_utc",
    "close_time_utc",
    "available_at_utc",
    "open",
    "high",
    "low",
    "close",
    "is_final",
)

TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}


def parse_timestamp(value: Any, field: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{field} must be an ISO-8601 string")
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        errors.append(f"{field} is not valid ISO-8601: {value!r}")
        return None
    if dt.tzinfo is None:
        errors.append(f"{field} must include a timezone")
        return None
    return dt.astimezone(timezone.utc)


def parse_decimal(value: Any, field: str, errors: list[str]) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f"{field} must be numeric")
        return None
    if not result.is_finite():
        errors.append(f"{field} must be finite")
        return None
    return result


def require_mapping(obj: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any] | None:
    value = obj.get(key)
    if not isinstance(value, dict):
        errors.append(f"{key} must be an object")
        return None
    return value


def validate_candles(
    timeframe: str,
    candles: Any,
    analysis_time: datetime,
    minimum_candles: int,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "input_count": 0,
        "usable_count": 0,
        "excluded_non_final": 0,
        "excluded_not_available": 0,
        "gaps": [],
        "latest_final_close_utc": None,
    }
    if not isinstance(candles, list):
        errors.append(f"timeframes.{timeframe} must be an array")
        return summary

    summary["input_count"] = len(candles)
    usable: list[tuple[datetime, datetime]] = []
    seen_open_times: set[datetime] = set()
    original_open_times: list[datetime] = []

    for index, candle in enumerate(candles):
        prefix = f"timeframes.{timeframe}[{index}]"
        if not isinstance(candle, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for field in CANDLE_FIELDS:
            if field not in candle:
                errors.append(f"{prefix}.{field} is required")

        open_time = parse_timestamp(candle.get("open_time_utc"), f"{prefix}.open_time_utc", errors)
        close_time = parse_timestamp(candle.get("close_time_utc"), f"{prefix}.close_time_utc", errors)
        available_at = parse_timestamp(candle.get("available_at_utc"), f"{prefix}.available_at_utc", errors)

        values: dict[str, Decimal | None] = {}
        for field in ("open", "high", "low", "close"):
            values[field] = parse_decimal(candle.get(field), f"{prefix}.{field}", errors)

        for field in ("base_volume", "quote_volume"):
            if field in candle and candle[field] is not None:
                volume = parse_decimal(candle[field], f"{prefix}.{field}", errors)
                if volume is not None and volume < 0:
                    errors.append(f"{prefix}.{field} must be non-negative")

        trade_count = candle.get("trade_count")
        if trade_count is not None and (not isinstance(trade_count, int) or trade_count < 0):
            errors.append(f"{prefix}.trade_count must be a non-negative integer")

        if all(values[field] is not None for field in values):
            open_price = values["open"]
            high_price = values["high"]
            low_price = values["low"]
            close_price = values["close"]
            assert open_price is not None and high_price is not None
            assert low_price is not None and close_price is not None
            if min(open_price, high_price, low_price, close_price) <= 0:
                errors.append(f"{prefix} OHLC prices must be positive")
            if high_price < low_price:
                errors.append(f"{prefix}.high must be >= low")
            if high_price < max(open_price, close_price):
                errors.append(f"{prefix}.high must be >= open and close")
            if low_price > min(open_price, close_price):
                errors.append(f"{prefix}.low must be <= open and close")

        if open_time is not None:
            original_open_times.append(open_time)
            if open_time in seen_open_times:
                errors.append(f"{prefix}.open_time_utc is duplicated")
            seen_open_times.add(open_time)

        if open_time and close_time and available_at:
            if not open_time < close_time:
                errors.append(f"{prefix} must satisfy open_time < close_time")
            if close_time > available_at:
                errors.append(f"{prefix} must satisfy close_time <= available_at")

            is_final = candle.get("is_final") is True
            if not is_final:
                summary["excluded_non_final"] += 1
            elif available_at > analysis_time or close_time > analysis_time:
                summary["excluded_not_available"] += 1
            else:
                usable.append((open_time, close_time))

    if original_open_times != sorted(original_open_times):
        warnings.append(f"timeframes.{timeframe} is not in ascending open-time order")

    usable.sort(key=lambda item: item[0])
    summary["usable_count"] = len(usable)
    if len(usable) < minimum_candles:
        errors.append(
            f"timeframes.{timeframe} has {len(usable)} usable candles; "
            f"minimum is {minimum_candles}"
        )

    if usable:
        summary["latest_final_close_utc"] = usable[-1][1].isoformat().replace("+00:00", "Z")

    expected_seconds = TIMEFRAME_SECONDS.get(timeframe)
    if expected_seconds and len(usable) > 1:
        for previous, current in zip(usable, usable[1:]):
            delta = (current[0] - previous[0]).total_seconds()
            if delta > expected_seconds * 1.5:
                gap = {
                    "after_open_utc": previous[0].isoformat().replace("+00:00", "Z"),
                    "next_open_utc": current[0].isoformat().replace("+00:00", "Z"),
                    "seconds": delta,
                }
                summary["gaps"].append(gap)
        if summary["gaps"]:
            warnings.append(f"timeframes.{timeframe} contains {len(summary['gaps'])} material gap(s)")

        if usable:
            age = (analysis_time - usable[-1][1]).total_seconds()
            if age > expected_seconds * 2.5:
                warnings.append(
                    f"latest usable {timeframe} candle is {age:.0f}s old, "
                    "more than 2.5 timeframe intervals"
                )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", nargs="?", help="Path to JSON snapshot; read stdin when omitted")
    args = parser.parse_args()

    try:
        if args.snapshot:
            payload = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        else:
            payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [f"Unable to read JSON: {exc}"], "warnings": []}, indent=2))
        return 2

    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "errors": ["Top-level JSON must be an object"], "warnings": []}, indent=2))
        return 1

    errors: list[str] = []
    warnings: list[str] = []

    analysis_time = parse_timestamp(payload.get("analysis_time_utc"), "analysis_time_utc", errors)
    if analysis_time is None:
        analysis_time = datetime.now(timezone.utc)

    mode = payload.get("mode", "paper")
    if mode not in {"analysis", "paper", "live"}:
        errors.append("mode must be analysis, paper, or live")

    venue = payload.get("venue")
    if not isinstance(venue, str) or not venue.strip():
        errors.append("venue is required")

    instrument = require_mapping(payload, "instrument", errors)
    product_type = None
    if instrument is not None:
        for key in ("symbol", "base_asset", "quote_asset", "product_type", "contract_type", "status"):
            if key not in instrument:
                errors.append(f"instrument.{key} is required")
        product_type = instrument.get("product_type")
        if product_type not in {"spot", "perpetual"}:
            errors.append("instrument.product_type must be spot or perpetual")
        if instrument.get("contract_type") not in {"spot", "linear", "inverse"}:
            errors.append("instrument.contract_type must be spot, linear, or inverse")
        if mode == "live" and instrument.get("status") != "TRADING":
            errors.append("instrument.status must be TRADING in live mode")

    signal_timeframe = payload.get("signal_timeframe")
    if not isinstance(signal_timeframe, str) or not signal_timeframe:
        errors.append("signal_timeframe is required")

    timeframes = payload.get("timeframes")
    timeframe_summary: dict[str, Any] = {}
    risk_policy = payload.get("risk_policy") if isinstance(payload.get("risk_policy"), dict) else {}
    minimum_candles_raw = risk_policy.get("minimum_candles", payload.get("minimum_candles", 100))
    try:
        minimum_candles = int(minimum_candles_raw)
    except (ValueError, TypeError):
        minimum_candles = 100
        errors.append("minimum_candles must be an integer")
    if minimum_candles < 1:
        errors.append("minimum_candles must be positive")
        minimum_candles = 1

    if not isinstance(timeframes, dict) or not timeframes:
        errors.append("timeframes must be a non-empty object")
    else:
        for timeframe, candles in timeframes.items():
            timeframe_summary[str(timeframe)] = validate_candles(
                str(timeframe), candles, analysis_time, minimum_candles, errors, warnings
            )
        if isinstance(signal_timeframe, str) and signal_timeframe not in timeframes:
            errors.append("signal_timeframe is not present in timeframes")
        regime_timeframe = payload.get("regime_timeframe")
        if regime_timeframe and regime_timeframe not in timeframes:
            errors.append("regime_timeframe is not present in timeframes")

    book = payload.get("book")
    if mode in {"paper", "live"} and not isinstance(book, dict):
        message = "book is required for paper/live order planning"
        if mode == "live":
            errors.append(message)
        else:
            warnings.append(message)
    elif isinstance(book, dict):
        bid = parse_decimal(book.get("bid"), "book.bid", errors)
        ask = parse_decimal(book.get("ask"), "book.ask", errors)
        book_time = parse_timestamp(book.get("timestamp_utc"), "book.timestamp_utc", errors)
        if bid is not None and ask is not None:
            if bid <= 0 or ask <= 0:
                errors.append("book bid and ask must be positive")
            if bid > ask:
                errors.append("book.bid must be <= book.ask")
        if book_time is not None:
            if book_time > analysis_time:
                errors.append("book.timestamp_utc cannot be after analysis_time_utc")
            max_age = risk_policy.get("max_data_age_seconds")
            if max_age is not None:
                try:
                    if (analysis_time - book_time).total_seconds() > float(max_age):
                        errors.append("book data exceeds risk_policy.max_data_age_seconds")
                except (ValueError, TypeError):
                    errors.append("risk_policy.max_data_age_seconds must be numeric")

    if mode == "live":
        execution = require_mapping(payload, "execution", errors)
        if execution is not None and execution.get("authorized") is not True:
            errors.append("execution.authorized must be true in live mode")

        account = require_mapping(payload, "account", errors)
        exchange_rules = require_mapping(payload, "exchange_rules", errors)
        costs = require_mapping(payload, "costs", errors)
        live_risk = require_mapping(payload, "risk_policy", errors)

        if live_risk is not None:
            for field in LIVE_RISK_FIELDS:
                if field not in live_risk:
                    errors.append(f"risk_policy.{field} is required in live mode")

        if exchange_rules is not None:
            for field in ("tick_size", "quantity_step", "min_quantity", "min_notional"):
                if field not in exchange_rules:
                    errors.append(f"exchange_rules.{field} is required in live mode")

        if costs is not None:
            for field in ("maker_fee_bps", "taker_fee_bps"):
                if field not in costs:
                    errors.append(f"costs.{field} is required in live mode")

        if account is not None:
            for field in ("timestamp_utc", "equity", "open_positions", "open_orders"):
                if field not in account:
                    errors.append(f"account.{field} is required in live mode")
            account_time = parse_timestamp(account.get("timestamp_utc"), "account.timestamp_utc", errors)
            max_account_age = risk_policy.get("max_account_age_seconds")
            if account_time is not None and max_account_age is not None:
                try:
                    if (analysis_time - account_time).total_seconds() > float(max_account_age):
                        errors.append("account data exceeds risk_policy.max_account_age_seconds")
                except (ValueError, TypeError):
                    errors.append("risk_policy.max_account_age_seconds must be numeric")

    if product_type == "perpetual":
        derivatives = payload.get("derivatives")
        if not isinstance(derivatives, dict):
            message = "derivatives block is required for perpetual instruments"
            if mode == "live":
                errors.append(message)
            else:
                warnings.append(message)
        else:
            for field in (
                "timestamp_utc",
                "mark_price",
                "index_price",
                "funding_rate",
                "next_funding_time_utc",
                "maintenance_margin_rate",
                "margin_mode",
                "current_leverage",
            ):
                if field not in derivatives:
                    errors.append(f"derivatives.{field} is required for perpetual instruments")
            for field in ("mark_price", "index_price"):
                value = parse_decimal(derivatives.get(field), f"derivatives.{field}", errors)
                if value is not None and value <= 0:
                    errors.append(f"derivatives.{field} must be positive")
            parse_timestamp(derivatives.get("timestamp_utc"), "derivatives.timestamp_utc", errors)
            parse_timestamp(
                derivatives.get("next_funding_time_utc"),
                "derivatives.next_funding_time_utc",
                errors,
            )

    report = {
        "ok": not errors,
        "mode": mode,
        "analysis_time_utc": analysis_time.isoformat().replace("+00:00", "Z"),
        "errors": errors,
        "warnings": warnings,
        "timeframes": timeframe_summary,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
