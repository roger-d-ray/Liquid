# Input Contract

Use this contract as the canonical runtime shape. Adapt venue-specific payloads into it before analysis. Do not silently invent missing fields.

## Required top-level fields

```json
{
  "analysis_time_utc": "2026-07-27T12:00:00Z",
  "mode": "paper",
  "venue": "example-exchange",
  "instrument": {
    "symbol": "BTCUSDT",
    "base_asset": "BTC",
    "quote_asset": "USDT",
    "product_type": "perpetual",
    "contract_type": "linear",
    "status": "TRADING",
    "listing_time_utc": "2020-01-01T00:00:00Z"
  },
  "strategy": "breakout_time_series",
  "signal_timeframe": "4h",
  "regime_timeframe": "1d",
  "timeframes": {
    "4h": [],
    "1d": []
  }
}
```

Allowed values:

- `mode`: `analysis`, `paper`, `live`
- `product_type`: `spot`, `perpetual`
- `contract_type`: `spot`, `linear`, `inverse`
- `strategy`: `breakout_time_series`, `pullback_continuation`, `cross_sectional_rotation`

Use `contract_type=spot` for spot instruments. The bundled sizing script supports only `spot` and `linear`.

## Candle schema

Store candles under `timeframes[timeframe]` in ascending open-time order:

```json
{
  "open_time_utc": "2026-07-27T04:00:00Z",
  "close_time_utc": "2026-07-27T07:59:59.999Z",
  "available_at_utc": "2026-07-27T08:00:00.250Z",
  "open": "116000.0",
  "high": "117200.0",
  "low": "115700.0",
  "close": "116900.0",
  "base_volume": "1234.56",
  "quote_volume": "143000000.0",
  "trade_count": 54321,
  "is_final": true,
  "source": "exchange-websocket"
}
```

Require:

- `open_time_utc < close_time_utc <= available_at_utc`
- `available_at_utc <= analysis_time_utc` for every candle used
- `is_final=true` for every candle used
- `high >= max(open, close, low)`
- `low <= min(open, close, high)`
- non-negative volume and trade count
- unique open times
- consistent timeframe spacing, allowing only declared venue outages or maintenance gaps

Keep non-final candles separate when possible. If a payload includes them, ignore them for all indicators, breakouts, ranks, swings, and signals.

Require enough final history for the longest indicator plus a warm-up margin. Use at least 100 final bars by default; prefer 250 for backtests and regime calculations.

## Market microstructure snapshot

Require this block for paper or live order planning:

```json
{
  "book": {
    "timestamp_utc": "2026-07-27T12:00:00Z",
    "bid": "116890.0",
    "bid_size": "2.10",
    "ask": "116900.0",
    "ask_size": "1.75",
    "depth_notional_within_10bps": "3500000.0"
  }
}
```

Calculate:

```text
mid = (bid + ask) / 2
spread_bps = (ask - bid) / mid * 10000
```

Do not infer executable liquidity from the last-trade price. Refresh the book immediately before live submission.

## Venue trading rules

Require current rules for live execution:

```json
{
  "exchange_rules": {
    "tick_size": "0.10",
    "quantity_step": "0.001",
    "min_quantity": "0.001",
    "max_quantity": "1000",
    "min_notional": "5.00",
    "max_notional": "250000",
    "allowed_order_types": ["LIMIT", "MARKET", "STOP_MARKET"],
    "supports_reduce_only": true,
    "supports_attached_stop": true,
    "supports_order_preview": true
  }
}
```

Treat exchange filters as time-varying. Refresh them periodically and after a rejected order. Never round by decimal display precision alone; use tick and step filters.

## Costs

Require current account-tier costs for live sizing:

```json
{
  "costs": {
    "maker_fee_bps": "2.0",
    "taker_fee_bps": "5.0",
    "estimated_entry_slippage_bps": "2.0",
    "estimated_exit_slippage_bps": "3.0"
  }
}
```

Use conservative taker costs unless the execution plan can reasonably guarantee maker treatment. Record actual fees after fill.

## Account and position state

Require fresh account data for live mode:

```json
{
  "account": {
    "timestamp_utc": "2026-07-27T12:00:00Z",
    "equity": "10000.00",
    "free_cash": "8000.00",
    "free_collateral": "8000.00",
    "daily_realized_pnl": "-25.00",
    "daily_unrealized_pnl": "10.00",
    "peak_equity": "10300.00",
    "open_positions": [],
    "open_orders": []
  }
}
```

Every open position should include:

- symbol and side
- verified quantity
- average entry price
- mark price for derivatives
- liquidation price when available
- leverage and margin mode
- protective stop order ID and status
- unrealized PnL

Every open order should include:

- exchange order ID
- client order ID
- symbol, side, type, quantity, filled quantity, price, and status
- `reduce_only` state where applicable
- creation and update timestamps

If open-order or position state is incomplete or inconsistent, use `MANAGE_ONLY`.

## Risk policy

Require explicit values for live mode:

```json
{
  "risk_policy": {
    "risk_fraction_per_trade": "0.0025",
    "max_total_open_risk_fraction": "0.01",
    "max_daily_loss_fraction": "0.01",
    "max_drawdown_fraction": "0.05",
    "max_leverage": "2.0",
    "max_positions": 4,
    "max_symbol_notional_fraction": "0.20",
    "max_spread_bps": "8.0",
    "max_slippage_bps": "10.0",
    "max_data_age_seconds": 10,
    "max_account_age_seconds": 10,
    "min_24h_quote_volume": "10000000",
    "min_listing_age_days": 90,
    "stablecoin_depeg_threshold_fraction": "0.005",
    "entry_timeout_seconds": 30,
    "entry_timeout_bars": 1
  }
}
```

These numbers are examples, not mandatory live defaults. If a live value is absent, return `NO_TRADE` rather than selecting one silently.

Paper mode may use clearly labeled simulation defaults, but report every default used.

## Perpetual-futures block

Require this block for perpetuals:

```json
{
  "derivatives": {
    "timestamp_utc": "2026-07-27T12:00:00Z",
    "mark_price": "116895.0",
    "index_price": "116880.0",
    "funding_rate": "0.0001",
    "next_funding_time_utc": "2026-07-27T16:00:00Z",
    "funding_interval_hours": 8,
    "maintenance_margin_rate": "0.005",
    "margin_mode": "ISOLATED",
    "current_leverage": "2.0",
    "open_interest": "2500000000"
  }
}
```

Use mark and index values only according to the venue's documented definitions. Know which price triggers stops and which price drives liquidation. Do not assume last price, mark price, and index price are interchangeable.

Estimate funding over the expected holding period:

```text
estimated_funding = notional * funding_rate * expected_funding_events
```

Use the correct sign for long and short positions. Treat unusually high funding or a large mark-index basis as a risk warning, not a standalone entry instruction.

## Stablecoin and collateral health

When the quote or collateral is a stablecoin, include a fresh reference price and source:

```json
{
  "collateral_health": {
    "asset": "USDT",
    "reference_price_usd": "0.9998",
    "timestamp_utc": "2026-07-27T12:00:00Z",
    "source_count": 3
  }
}
```

Block new positions when the absolute deviation from the configured reference exceeds the risk-policy threshold or when the reference is stale or unreliable.

## Cross-sectional universe

For rotation, supply a point-in-time universe:

```json
{
  "universe": {
    "as_of_utc": "2026-07-27T00:00:00Z",
    "venue": "example-exchange",
    "quote_asset": "USDT",
    "product_type": "spot",
    "symbols": [
      {
        "symbol": "BTCUSDT",
        "listing_time_utc": "2020-01-01T00:00:00Z",
        "status": "TRADING",
        "quote_volume_24h": "1000000000",
        "spread_bps": "0.5",
        "history_complete": true
      }
    ]
  }
}
```

Do not reconstruct a historical backtest from today's symbol list. Include delisted symbols and historical trading rules where data permits.

## Freshness and clock rules

- Use UTC everywhere.
- Compare source timestamps to exchange server time, not only local system time.
- Declare maximum permissible age by data type.
- Treat a large clock offset, reconnect gap, sequence gap, or missing account update as a hard execution failure.
- Preserve raw timestamps and the normalized timestamps used by the strategy.

## Missing-data response

When mandatory data is missing, return:

```json
{
  "decision": "NO_TRADE",
  "data_quality": "FAIL",
  "missing_fields": ["risk_policy.max_slippage_bps"],
  "reason": "Live execution requires an explicit slippage cap."
}
```
