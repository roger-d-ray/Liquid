"""Canonical OHLCV models and venue-response normalisation.

All timestamps are UTC Unix epoch milliseconds.  Keeping one unit at the API
boundary avoids timezone-dependent date arithmetic in consumers.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable


def utc_ms(value: datetime | int | float) -> int:
    """Return a UTC epoch-millisecond timestamp."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime is not a valid venue timestamp")
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    value = int(value)
    # Venue APIs sometimes expose seconds and sometimes milliseconds.
    return value * 1000 if abs(value) < 100_000_000_000 else value


@dataclass(frozen=True)
class ClosedBar:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    received_at: int
    available_at: int
    aggregation_method: str = 'native'
    completeness: str = 'complete'
    quality_flags: tuple[str, ...] = ()
    component_count: int = 1
    expected_component_count: int = 1
    missing_component_open_times: tuple[int, ...] = ()
    is_final: bool = field(default=True, init=False)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class IncompleteBar(ClosedBar):
    completeness: str = field(default='incomplete', init=False)
    is_final: bool = field(default=False, init=False)


@dataclass(frozen=True)
class IntrabarSnapshot:
    snapshot_time: int
    bar_open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    aggregation_method: str = 'native'
    completeness: str = 'complete'
    quality_flags: tuple[str, ...] = ()
    component_count: int = 1
    expected_component_count: int = 1
    missing_component_open_times: tuple[int, ...] = ()
    is_final: bool = field(default=False, init=False)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedBars:
    closed: tuple[ClosedBar, ...]
    intrabar: tuple[IntrabarSnapshot, ...]
    incomplete: tuple[IncompleteBar, ...] = ()


def _prices(row: dict) -> tuple[float, float, float, float, float]:
    values = tuple(float(row[key]) for key in ("open", "high", "low", "close", "volume"))
    open_, high, low, close, volume = values
    if volume < 0 or low > high or not low <= open_ <= high or not low <= close <= high:
        raise ValueError(f"invalid OHLCV values for candle at {row.get('open_time')}")
    return values


def normalize_ohlcv(
    rows: Iterable[dict],
    timeframe_seconds: int,
    source: str,
    *,
    observed_at: datetime | int | float,
    received_at: datetime | int | float,
    grace_period_seconds: float = 2.0,
    existing_closed: Iterable[ClosedBar] = (),
) -> NormalizedBars:
    """Split an ordered venue response into immutable bars and live snapshots.

    ``observed_at`` should be venue server time where available. Duplicate live
    rows replace the earlier snapshot from the same response. Duplicate final
    rows, and rows matching ``existing_closed``, never overwrite the first
    consolidated value.
    """
    if timeframe_seconds <= 0:
        raise ValueError("timeframe_seconds must be positive")
    if grace_period_seconds < 0:
        raise ValueError("grace_period_seconds cannot be negative")

    now_ms = utc_ms(observed_at)
    received_ms = utc_ms(received_at)
    duration_ms = int(timeframe_seconds * 1000)
    grace_ms = int(grace_period_seconds * 1000)
    closed_by_time = {bar.open_time: bar for bar in existing_closed}
    snapshots_by_time: dict[int, IntrabarSnapshot] = {}
    incomplete_by_time: dict[int, IncompleteBar] = {}
    previous_open: int | None = None

    for row in rows:
        open_time = utc_ms(row["open_time"])
        if open_time > now_ms:
            raise ValueError(f"future candle timestamp: {open_time} > {now_ms}")
        if previous_open is not None and open_time < previous_open:
            raise ValueError(f"out-of-sequence candle: {open_time} after {previous_open}")
        previous_open = open_time

        open_, high, low, close, volume = _prices(row)
        close_time = open_time + duration_ms
        final_after = close_time + grace_ms
        method = str(row.get('aggregation_method', 'native'))
        completeness = str(row.get('completeness', 'complete'))
        if completeness not in {'complete', 'incomplete'}:
            raise ValueError(f'invalid completeness at {open_time}')
        flags = tuple(str(flag) for flag in row.get('quality_flags', ()))
        component_count = int(row.get('component_count', 1))
        expected_count = int(row.get('expected_component_count', 1))
        missing_times = tuple(
            utc_ms(ts) for ts in row.get('missing_component_open_times', ())
        )

        if now_ms >= final_after:
            bar_type = IncompleteBar if completeness == 'incomplete' else ClosedBar
            target = incomplete_by_time if completeness == 'incomplete' else closed_by_time
            if open_time not in target:
                target[open_time] = bar_type(
                    open_time=open_time,
                    close_time=close_time,
                    open=open_, high=high, low=low, close=close, volume=volume,
                    source=source,
                    received_at=received_ms,
                    available_at=max(final_after, received_ms),
                    aggregation_method=method,
                    quality_flags=flags,
                    component_count=component_count,
                    expected_component_count=expected_count,
                    missing_component_open_times=missing_times,
                )
            # A final value always wins over a snapshot of the same bar, while
            # an existing final value itself remains immutable.
            snapshots_by_time.pop(open_time, None)
        elif open_time not in closed_by_time:
            snapshots_by_time[open_time] = IntrabarSnapshot(
                snapshot_time=received_ms,
                bar_open_time=open_time,
                open=open_, high=high, low=low, close=close, volume=volume,
                source=source,
                aggregation_method=method,
                completeness=completeness,
                quality_flags=flags,
                component_count=component_count,
                expected_component_count=expected_count,
                missing_component_open_times=missing_times,
            )

    return NormalizedBars(
        closed=tuple(closed_by_time[key] for key in sorted(closed_by_time)),
        intrabar=tuple(snapshots_by_time[key] for key in sorted(snapshots_by_time)),
        incomplete=tuple(incomplete_by_time[key] for key in sorted(incomplete_by_time)),
    )


def only_available_closed_bars(
    candles: Iterable[ClosedBar | dict],
    *,
    as_of: datetime | int | float | None = None,
) -> list[dict]:
    """Return only final bars whose data was available at ``as_of``.

    This is the single gate used by indicator and bar-close signal consumers.
    Missing ``is_final`` is deliberately not treated as final.
    """
    cutoff = utc_ms(as_of) if as_of is not None else None
    result = []
    for candle in candles:
        item = candle.to_dict() if isinstance(candle, ClosedBar) else dict(candle)
        if item.get('completeness', 'complete') != 'complete':
            continue
        if item.get("is_final") is not True:
            continue
        available_at = item.get("available_at")
        if available_at is None:
            continue
        if cutoff is not None and utc_ms(available_at) > cutoff:
            continue
        result.append(item)
    return result
