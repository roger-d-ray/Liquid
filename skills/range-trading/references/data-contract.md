# Data Contract

Use this reference whenever market-data shape, freshness, or trading constraints are unclear.

## Required analysis inputs

Require these fields for a current or historical range-trading decision:

- `asset`: symbol or instrument name.
- `venue`: exchange, broker, or data venue when relevant.
- `instrument_type`: spot, equity, ETF, future, perpetual, CFD, or forex pair.
- `timeframe`: primary candle interval.
- `analysis_time`: ISO-8601 timestamp with timezone.
- `bars`: chronologically sortable OHLC data containing:
  - `timestamp`
  - `open`
  - `high`
  - `low`
  - `close`
  - `is_final`
  - `available_at`
  - `volume` when meaningful and available.

Treat `available_at` as the earliest timestamp at which that finalized row could have been known. This protects current analysis and historical testing from look-ahead bias.

## Recommended optional inputs

- `context_timeframe`: normally 4–6 times the primary timeframe.
- `current_quote`: live bid, ask, or last price; use for context only.
- `tick_size` and `contract_multiplier`.
- `allowed_directions`: `long_only`, `exit_only`, or `long_short`.
- `spread`, entry and exit fees, expected slippage, funding, and borrow cost.
- `account_equity`, `risk_budget`, or `risk_percent` for position sizing.
- `event_calendar` from a trustworthy source.
- source name and whether prices are adjusted for corporate actions.

## Accepted JSON shapes for the helper script

The script accepts one of these forms:

```json
[
  {
    "timestamp": "2026-07-01T10:00:00Z",
    "open": 100.0,
    "high": 101.0,
    "low": 99.5,
    "close": 100.5,
    "volume": 1200,
    "is_final": true,
    "available_at": "2026-07-01T11:00:02Z"
  }
]
```

```json
{
  "bars": [
    {
      "timestamp": "2026-07-01T10:00:00Z",
      "open": 100.0,
      "high": 101.0,
      "low": 99.5,
      "close": 100.5,
      "is_final": true,
      "available_at": "2026-07-01T11:00:02Z"
    }
  ]
}
```

```json
{
  "timeframes": {
    "1h": [
      {
        "timestamp": "2026-07-01T10:00:00Z",
        "open": 100.0,
        "high": 101.0,
        "low": 99.5,
        "close": 100.5,
        "is_final": true,
        "available_at": "2026-07-01T11:00:02Z"
      }
    ]
  }
}
```

For CSV input, require equivalent column names.

## Finality and live-price policy

- Filter out every row where `is_final` is false.
- Filter out every row where `available_at` is later than `analysis_time`.
- Never infer finality from row order or from the fact that a REST endpoint returned a row.
- Never compute indicators with a partially formed candle.
- A live quote may show whether price is approaching an entry zone, but the setup remains conditional until the required finalized-candle trigger occurs.

## History requirements

- Require at least 80 usable finalized bars for a limited assessment.
- Prefer 150–200 bars for stable indicator and pivot estimates.
- Require enough bars before the analysis window to warm up ADX, ATR, RSI, moving averages, and Bollinger Bands.
- If the chosen lookback is shorter than the age of the suspected range, disclose that the boundary estimate may be truncated.

## Validation rules

- Parse all timestamps in one timezone, preferably UTC.
- Sort bars ascending by timestamp.
- Resolve duplicates explicitly; prefer the row with the latest valid `available_at` when values are revisions from the same source.
- Reject bars where `high < max(open, close, low)` or `low > min(open, close, high)`.
- Reject non-positive prices for instruments that cannot trade below zero.
- Do not mix venues, adjusted and unadjusted equity data, or different contract rolls without disclosure.
- Do not reconstruct OHLCV from news articles, search snippets, screenshots, or approximate narrative data.

## Missing-data behavior

- Missing OHLC or finality fields: return **Insufficient data**.
- Missing volume: continue without volume confirmation and say so.
- Missing costs: calculate gross R:R, mark net R:R as not computable, and avoid calling the setup economically validated.
- Missing short availability: interpret a resistance signal as exit or avoid-long guidance, not an automatic short.
- Missing account risk inputs: provide the sizing formula only; do not invent a quantity.
