#!/usr/bin/env python3
"""Compute closed-bar diagnostics for the range-trading skill.

This helper validates candle finality and calculates indicators plus pivot-price
clusters. It intentionally does not emit a buy/sell recommendation.

Usage:
    python scripts/compute_range_metrics.py bars.json \
        --analysis-time 2026-07-28T12:00:00Z \
        --timeframe 1h \
        --output diagnostics.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]
    is_final: bool
    available_at: datetime


def parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if abs(seconds) > 10_000_000_000:  # probable milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"invalid {field}: {value!r}") from exc

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {field}")

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    raise ValueError(f"invalid {field}: {value!r}")


def parse_float(value: Any, field: str, *, optional: bool = False) -> Optional[float]:
    if optional and (value is None or value == ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite {field}: {value!r}")
    return number


def load_rows(path: Path, timeframe: Optional[str]) -> tuple[list[dict[str, Any]], Optional[str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle)), timeframe

    if suffix not in {".json", ".jsonl"}:
        raise ValueError("input must be CSV, JSON, or JSONL")

    if suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError(f"JSONL row {line_number} is not an object")
                rows.append(item)
        return rows, timeframe

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            raise ValueError("JSON array must contain bar objects")
        return payload, timeframe

    if not isinstance(payload, dict):
        raise ValueError("JSON must be an array or object")

    if isinstance(payload.get("bars"), list):
        rows = payload["bars"]
        if not all(isinstance(item, dict) for item in rows):
            raise ValueError("bars must contain objects")
        return rows, timeframe or payload.get("timeframe")

    timeframes = payload.get("timeframes")
    if isinstance(timeframes, dict):
        selected = timeframe
        if selected is None:
            keys = list(timeframes.keys())
            if len(keys) != 1:
                raise ValueError("--timeframe is required when JSON contains multiple timeframes")
            selected = str(keys[0])
        rows = timeframes.get(selected)
        if not isinstance(rows, list):
            raise ValueError(f"timeframe {selected!r} not found or is not a list")
        if not all(isinstance(item, dict) for item in rows):
            raise ValueError(f"timeframe {selected!r} must contain bar objects")
        return rows, selected

    raise ValueError("JSON object must contain 'bars' or 'timeframes'")


def parse_bar(row: dict[str, Any], row_number: int) -> Bar:
    prefix = f"row {row_number}"
    timestamp = parse_datetime(row.get("timestamp"), f"{prefix}.timestamp")
    available_at = parse_datetime(row.get("available_at"), f"{prefix}.available_at")
    open_price = parse_float(row.get("open"), f"{prefix}.open")
    high = parse_float(row.get("high"), f"{prefix}.high")
    low = parse_float(row.get("low"), f"{prefix}.low")
    close = parse_float(row.get("close"), f"{prefix}.close")
    volume = parse_float(row.get("volume"), f"{prefix}.volume", optional=True)
    is_final = parse_bool(row.get("is_final"), f"{prefix}.is_final")

    assert open_price is not None and high is not None and low is not None and close is not None
    if high < max(open_price, close, low):
        raise ValueError(f"{prefix}: high is below another OHLC value")
    if low > min(open_price, close, high):
        raise ValueError(f"{prefix}: low is above another OHLC value")
    if volume is not None and volume < 0:
        raise ValueError(f"{prefix}: volume cannot be negative")

    return Bar(
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_final=is_final,
        available_at=available_at,
    )


def prepare_bars(rows: list[dict[str, Any]], analysis_time: datetime) -> tuple[list[Bar], list[str], dict[str, int]]:
    parsed = [parse_bar(row, index) for index, row in enumerate(rows, start=1)]
    excluded_non_final = sum(not bar.is_final for bar in parsed)
    excluded_unavailable = sum(bar.is_final and bar.available_at > analysis_time for bar in parsed)

    eligible = [
        bar for bar in parsed
        if bar.is_final and bar.available_at <= analysis_time
    ]

    by_timestamp: dict[datetime, Bar] = {}
    duplicate_count = 0
    for bar in eligible:
        existing = by_timestamp.get(bar.timestamp)
        if existing is None:
            by_timestamp[bar.timestamp] = bar
            continue
        duplicate_count += 1
        if bar.available_at >= existing.available_at:
            by_timestamp[bar.timestamp] = bar

    bars = sorted(by_timestamp.values(), key=lambda bar: bar.timestamp)
    warnings: list[str] = []
    if excluded_non_final:
        warnings.append(f"excluded {excluded_non_final} non-final row(s)")
    if excluded_unavailable:
        warnings.append(f"excluded {excluded_unavailable} row(s) unavailable at analysis_time")
    if duplicate_count:
        warnings.append(
            f"resolved {duplicate_count} duplicate timestamp row(s) by latest eligible available_at"
        )

    counts = {
        "input_rows": len(parsed),
        "usable_final_rows": len(bars),
        "excluded_non_final": excluded_non_final,
        "excluded_unavailable": excluded_unavailable,
        "resolved_duplicates": duplicate_count,
    }
    return bars, warnings, counts


def simple_moving_average(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be positive")
    rolling = 0.0
    for index, value in enumerate(values):
        rolling += value
        if index >= period:
            rolling -= values[index - period]
        if index >= period - 1:
            result[index] = rolling / period
    return result


def rolling_std(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        result[index] = statistics.pstdev(window)
    return result


def true_ranges(bars: list[Bar]) -> list[float]:
    values: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            values.append(bar.high - bar.low)
        else:
            previous_close = bars[index - 1].close
            values.append(max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            ))
    return values


def wilder_average(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    average = sum(values[:period]) / period
    result[period - 1] = average
    for index in range(period, len(values)):
        average = ((average * (period - 1)) + values[index]) / period
        result[index] = average
    return result


def rsi_wilder(closes: list[float], period: int = 14) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return result

    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains[index] = max(change, 0.0)
        losses[index] = max(-change, 0.0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    def value(gain: float, loss: float) -> float:
        if loss == 0 and gain == 0:
            return 50.0
        if loss == 0:
            return 100.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = value(avg_gain, avg_loss)
    for index in range(period + 1, len(closes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        result[index] = value(avg_gain, avg_loss)
    return result


def adx_wilder(bars: list[Bar], period: int = 14) -> list[Optional[float]]:
    count = len(bars)
    result: list[Optional[float]] = [None] * count
    if count <= period * 2:
        return result

    tr = [0.0] * count
    plus_dm = [0.0] * count
    minus_dm = [0.0] * count
    tr[0] = bars[0].high - bars[0].low

    for index in range(1, count):
        up_move = bars[index].high - bars[index - 1].high
        down_move = bars[index - 1].low - bars[index].low
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0.0
        tr[index] = max(
            bars[index].high - bars[index].low,
            abs(bars[index].high - bars[index - 1].close),
            abs(bars[index].low - bars[index - 1].close),
        )

    smoothed_tr = sum(tr[1:period + 1])
    smoothed_plus = sum(plus_dm[1:period + 1])
    smoothed_minus = sum(minus_dm[1:period + 1])
    dx: list[Optional[float]] = [None] * count

    for index in range(period, count):
        if index > period:
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr[index]
            smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[index]
            smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[index]

        if smoothed_tr == 0:
            dx[index] = 0.0
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        dx[index] = 0.0 if denominator == 0 else 100.0 * abs(plus_di - minus_di) / denominator

    first_adx_index = (period * 2) - 1
    initial_dx = [value for value in dx[period:first_adx_index + 1] if value is not None]
    if len(initial_dx) != period:
        return result

    adx = sum(initial_dx) / period
    result[first_adx_index] = adx
    for index in range(first_adx_index + 1, count):
        assert dx[index] is not None
        adx = ((adx * (period - 1)) + dx[index]) / period
        result[index] = adx
    return result


def efficiency_ratio(closes: list[float], period: int = 20) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(closes)
    for index in range(period, len(closes)):
        direction = abs(closes[index] - closes[index - period])
        volatility = sum(
            abs(closes[position] - closes[position - 1])
            for position in range(index - period + 1, index + 1)
        )
        result[index] = 0.0 if volatility == 0 else direction / volatility
    return result


def recent_median(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.median(clean) if clean else None


def detect_pivots(bars: list[Bar], window: int, start_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lows: list[dict[str, Any]] = []
    highs: list[dict[str, Any]] = []
    end = len(bars) - window
    for index in range(max(window, start_index), end):
        left = bars[index - window:index]
        right = bars[index + 1:index + window + 1]
        bar = bars[index]
        if bar.low < min(item.low for item in left) and bar.low <= min(item.low for item in right):
            lows.append({"index": index, "timestamp": bar.timestamp, "price": bar.low})
        if bar.high > max(item.high for item in left) and bar.high >= max(item.high for item in right):
            highs.append({"index": index, "timestamp": bar.timestamp, "price": bar.high})
    return lows, highs


def cluster_pivots(
    points: list[dict[str, Any]],
    tolerance: float,
    last_index: int,
    minimum_separation: int = 3,
) -> list[dict[str, Any]]:
    if not points:
        return []

    clusters: list[list[dict[str, Any]]] = []
    for point in sorted(points, key=lambda item: item["price"]):
        best_cluster: Optional[list[dict[str, Any]]] = None
        best_distance = math.inf
        for cluster in clusters:
            center = statistics.median(item["price"] for item in cluster)
            distance = abs(point["price"] - center)
            if distance <= tolerance and distance < best_distance:
                best_cluster = cluster
                best_distance = distance
        if best_cluster is None:
            clusters.append([point])
        else:
            best_cluster.append(point)

    output: list[dict[str, Any]] = []
    for cluster in clusters:
        ordered = sorted(cluster, key=lambda item: item["index"])
        separated: list[dict[str, Any]] = []
        for point in ordered:
            if not separated or point["index"] - separated[-1]["index"] >= minimum_separation:
                separated.append(point)
        prices = [item["price"] for item in cluster]
        center = statistics.median(prices)
        output.append({
            "center": center,
            "zone_low": center - tolerance,
            "zone_high": center + tolerance,
            "raw_pivot_count": len(cluster),
            "separated_pivot_count": len(separated),
            "price_span": max(prices) - min(prices),
            "first_timestamp": ordered[0]["timestamp"],
            "last_timestamp": ordered[-1]["timestamp"],
            "bars_since_last": last_index - ordered[-1]["index"],
            "timestamps": [item["timestamp"] for item in separated[-6:]],
        })

    output.sort(
        key=lambda item: (
            -item["separated_pivot_count"],
            item["bars_since_last"],
            item["price_span"],
        )
    )
    return output[:8]


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def round_value(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 8)
    if isinstance(value, datetime):
        return isoformat_z(value)
    if isinstance(value, list):
        return [round_value(item) for item in value]
    if isinstance(value, dict):
        return {key: round_value(item) for key, item in value.items()}
    return value


def build_diagnostics(
    bars: list[Bar],
    analysis_time: datetime,
    timeframe: Optional[str],
    warnings: list[str],
    counts: dict[str, int],
    pivot_window: int,
    range_lookback: int,
) -> dict[str, Any]:
    if len(bars) < 60:
        raise ValueError(
            f"only {len(bars)} usable finalized bars; at least 60 are required by the helper"
        )
    if len(bars) < 80:
        warnings.append("fewer than 80 finalized bars: skill verdict should be Insufficient data")
    elif len(bars) < 150:
        warnings.append("fewer than 150 finalized bars: treat range diagnostics as limited")

    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    tr = true_ranges(bars)
    atr = wilder_average(tr, 14)
    rsi = rsi_wilder(closes, 14)
    adx = adx_wilder(bars, 14)
    ma = simple_moving_average(closes, 50)
    er = efficiency_ratio(closes, 20)
    bb_mid = simple_moving_average(closes, 20)
    bb_std = rolling_std(closes, 20)

    last = len(bars) - 1
    atr_last = atr[last]
    ma_last = ma[last]
    rsi_last = rsi[last]
    adx_last = adx[last]
    er_last = er[last]

    ma_slope = None
    if atr_last and atr_last > 0 and last >= 10 and ma_last is not None and ma[last - 10] is not None:
        ma_slope = abs(ma_last - ma[last - 10]) / (10 * atr_last)

    prior_atr = recent_median(atr[max(0, last - 50):last])
    atr_ratio = None if atr_last is None or prior_atr in (None, 0) else atr_last / prior_atr

    bb_upper = None
    bb_lower = None
    bb_bandwidth = None
    bb_position = None
    if bb_mid[last] is not None and bb_std[last] is not None:
        bb_upper = bb_mid[last] + 2 * bb_std[last]
        bb_lower = bb_mid[last] - 2 * bb_std[last]
        if bb_mid[last] != 0:
            bb_bandwidth = (bb_upper - bb_lower) / bb_mid[last]
        if bb_upper != bb_lower:
            bb_position = (closes[last] - bb_lower) / (bb_upper - bb_lower)

    volume_ratio = None
    if volumes[last] is not None:
        prior_volume = recent_median(volumes[max(0, last - 20):last])
        if prior_volume not in (None, 0):
            volume_ratio = volumes[last] / prior_volume

    tolerance = max(
        0.25 * atr_last if atr_last is not None else 0.0,
        0.0015 * abs(closes[last]),
    )
    start_index = max(0, len(bars) - range_lookback)
    pivot_lows, pivot_highs = detect_pivots(bars, pivot_window, start_index)
    low_clusters = cluster_pivots(pivot_lows, tolerance, last)
    high_clusters = cluster_pivots(pivot_highs, tolerance, last)

    if not low_clusters or not high_clusters:
        warnings.append("no usable pivot cluster found on one or both sides")

    output: dict[str, Any] = {
        "purpose": "closed-bar diagnostics only; not a trade recommendation",
        "analysis_time": analysis_time,
        "timeframe": timeframe,
        "counts": counts,
        "warnings": warnings,
        "last_final_bar": {
            "timestamp": bars[last].timestamp,
            "available_at": bars[last].available_at,
            "open": bars[last].open,
            "high": bars[last].high,
            "low": bars[last].low,
            "close": bars[last].close,
            "volume": bars[last].volume,
        },
        "indicators": {
            "atr14": atr_last,
            "rsi14": rsi_last,
            "rsi14_previous": rsi[last - 1] if last > 0 else None,
            "adx14": adx_last,
            "adx14_previous": adx[last - 1] if last > 0 else None,
            "sma50": ma_last,
            "normalized_sma50_slope_10": ma_slope,
            "efficiency_ratio_20": er_last,
            "atr14_to_prior_50_median": atr_ratio,
            "bollinger20_mid": bb_mid[last],
            "bollinger20_upper": bb_upper,
            "bollinger20_lower": bb_lower,
            "bollinger20_bandwidth": bb_bandwidth,
            "bollinger20_position": bb_position,
            "volume_to_prior_20_median": volume_ratio,
        },
        "pivot_diagnostics": {
            "range_lookback_bars": min(range_lookback, len(bars)),
            "pivot_window_each_side": pivot_window,
            "zone_tolerance": tolerance,
            "note": (
                "separated_pivot_count is a clustering diagnostic, not a validated rejection count; "
                "confirm close-back-inside behavior and move-away distance manually"
            ),
            "pivot_low_clusters": low_clusters,
            "pivot_high_clusters": high_clusters,
        },
    }
    return round_value(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate finalized OHLCV and compute range-trading diagnostics."
    )
    parser.add_argument("input", type=Path, help="CSV, JSON, or JSONL candle file")
    parser.add_argument(
        "--analysis-time",
        required=True,
        help="ISO-8601 decision timestamp with timezone",
    )
    parser.add_argument(
        "--timeframe",
        help="timeframe key when the JSON contains a timeframes object",
    )
    parser.add_argument("--pivot-window", type=int, default=2)
    parser.add_argument("--range-lookback", type=int, default=120)
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.pivot_window < 1:
            raise ValueError("--pivot-window must be at least 1")
        if args.range_lookback < 20:
            raise ValueError("--range-lookback must be at least 20")
        if not args.input.exists():
            raise ValueError(f"input file not found: {args.input}")

        analysis_time = parse_datetime(args.analysis_time, "analysis_time")
        rows, detected_timeframe = load_rows(args.input, args.timeframe)
        bars, warnings, counts = prepare_bars(rows, analysis_time)
        diagnostics = build_diagnostics(
            bars=bars,
            analysis_time=analysis_time,
            timeframe=detected_timeframe or args.timeframe,
            warnings=warnings,
            counts=counts,
            pivot_window=args.pivot_window,
            range_lookback=args.range_lookback,
        )
        rendered = json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
