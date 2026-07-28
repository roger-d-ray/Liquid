# Output Schema

Return a concise human decision followed by one valid JSON object. Keep analysis and execution facts separate. Never output a trade quantity when the required account or risk inputs are missing.

## Human-readable format

Use this structure:

```markdown
## Crypto Momentum Decision - [VENUE] [SYMBOL] [SIGNAL_TIMEFRAME]

**Decision:** [NO_TRADE | WATCH | ENTER_LONG | ENTER_SHORT | HOLD | REDUCE | EXIT | MANAGE_ONLY]
**Mode:** [analysis | paper | live]
**Strategy:** [breakout_time_series | pullback_continuation | cross_sectional_rotation]
**Analysis time:** [UTC timestamp]

### Data quality
- Status: [PASS | WARN | FAIL]
- Latest final candle: [timestamp]
- Book/account age: [values or unavailable]
- Excluded data: [non-final bars, gaps, stale fields]

### Regime and setup
- Regime: [UPTREND | DOWNTREND | RANGE_OR_CHOP | INSUFFICIENT_DATA]
- Hard gates: [pass/fail with values]
- Optional context: [ADX, MACD, funding, open interest, news-risk veto]

### Trade plan
- Signal time: [timestamp]
- Planned entry method and range: [or none]
- Initial stop: [price and method, or none]
- Quantity/notional: [or unavailable]
- Monetary risk: [amount and equity fraction, or unavailable]
- Estimated fees/slippage/funding: [values or unavailable]
- Trailing/fade rule: [exact rule]

### Execution and safety
- Authorization: [not applicable | paper only | live verified | live missing]
- Failed gates: [complete list]
- Kill-switch state: [NORMAL | ENTRY_HALTED | MANAGE_ONLY | EMERGENCY]
- Next action: [one concrete action]
```

Do not use adjectives such as "guaranteed", "safe", "certain", or "high-confidence". Report measured gate results.

## Machine-readable JSON

Use this shape and valid JSON syntax:

```json
{
  "schema_version": "1.0",
  "decision_id": "unique-id",
  "analysis_time_utc": "2026-07-27T12:00:00Z",
  "mode": "paper",
  "venue": "example-exchange",
  "instrument": {
    "symbol": "BTCUSDT",
    "product_type": "perpetual",
    "contract_type": "linear",
    "signal_timeframe": "4h",
    "regime_timeframe": "1d"
  },
  "decision": "WATCH",
  "strategy": {
    "name": "breakout_time_series",
    "config_version": "breakout-4h-v1",
    "parameters": {
      "breakout_lookback_bars": 20,
      "rsi_period": 14,
      "rsi_long_min": 55,
      "atr_period": 14,
      "initial_stop_atr": 2.0,
      "trail_atr": 3.0
    }
  },
  "data_quality": {
    "status": "PASS",
    "latest_final_candle_utc": "2026-07-27T08:00:00Z",
    "market_data_age_seconds": 1.2,
    "account_data_age_seconds": 1.4,
    "excluded_non_final_bars": 1,
    "gaps": [],
    "missing_fields": []
  },
  "market": {
    "regime": "UPTREND",
    "bid": 116890.0,
    "ask": 116900.0,
    "spread_bps": 0.86,
    "mark_price": 116895.0,
    "index_price": 116880.0,
    "funding_rate": 0.0001,
    "next_funding_time_utc": "2026-07-27T16:00:00Z"
  },
  "indicators": {
    "close": 116900.0,
    "ema20": 115800.0,
    "ema50": 112500.0,
    "rsi14": 62.3,
    "atr14": 2100.0,
    "adx14": 24.1,
    "rvol20": 1.15,
    "extension_atr": 0.52
  },
  "gates": [
    {
      "name": "closed_candle",
      "required": true,
      "status": "PASS",
      "observed": true,
      "threshold": true
    },
    {
      "name": "breakout",
      "required": true,
      "status": "FAIL",
      "observed": 116900.0,
      "threshold": 117200.0
    }
  ],
  "risk": {
    "risk_fraction": null,
    "risk_budget": null,
    "entry_price": null,
    "stop_price": null,
    "quantity": null,
    "notional": null,
    "estimated_round_trip_cost": null,
    "estimated_funding": null,
    "liquidation_price": null,
    "liquidation_buffer_status": "NOT_APPLICABLE"
  },
  "order_plan": null,
  "position_management": null,
  "authorization": {
    "live_authorized": false,
    "scope_verified": false
  },
  "kill_switch": {
    "state": "NORMAL",
    "reasons": []
  },
  "failed_gates": ["breakout"],
  "warnings": [],
  "next_action": "Wait for a final 4h close above 117200 and re-evaluate execution conditions."
}
```

Use `null` for unavailable values. Do not use strings such as `N/A` in numeric fields.

## Order-plan object

Include only for `ENTER_LONG` or `ENTER_SHORT` after sizing:

```json
{
  "client_order_id": "decision-derived-id",
  "side": "BUY",
  "position_intent": "OPEN_LONG",
  "order_type": "LIMIT",
  "time_in_force": "IOC",
  "quantity": 0.008,
  "limit_price": 116910.0,
  "max_acceptable_fill_price": 116920.0,
  "entry_timeout_seconds": 30,
  "reduce_only": false,
  "protective_stop": {
    "side": "SELL",
    "trigger_price": 112700.0,
    "trigger_source": "MARK_PRICE",
    "order_type": "STOP_MARKET",
    "reduce_only": true
  }
}
```

Do not populate an order-plan object in analysis mode. In paper mode, mark it as simulated. In live mode, include the actual accepted exchange order ID only after the venue returns it.

## Position-management object

Include for `HOLD`, `REDUCE`, `EXIT`, or `MANAGE_ONLY`:

```json
{
  "verified_position_quantity": 0.008,
  "average_entry_price": 116900.0,
  "current_protective_stop": 114500.0,
  "calculated_trailing_stop": 115200.0,
  "proposed_stop": 115200.0,
  "stop_ratchets_toward_profit": true,
  "exit_reason": null,
  "reduce_only": true,
  "account_state_reconciled": true
}
```

## Rotation output

For cross-sectional rotation, add:

```json
{
  "universe_as_of_utc": "2026-07-27T00:00:00Z",
  "eligible_symbol_count": 60,
  "ranking_lookback_bars": 180,
  "rankings": [
    {
      "symbol": "BTCUSDT",
      "return": 0.18,
      "realized_volatility": 0.42,
      "rank": 1,
      "selected": true,
      "target_weight": 0.12
    }
  ],
  "estimated_turnover": 0.24,
  "estimated_trading_cost": 0.0012
}
```

State which symbols were excluded and why. Keep the output point-in-time.

## No-trade requirements

A `NO_TRADE` or `WATCH` result is complete only when it states:

- the exact failed or pending gate
- the observed value
- the required threshold or missing input
- the next condition that would justify re-evaluation

Do not generate an entry, quantity, or stop merely to make the response appear actionable.

## Live execution acknowledgement

After a live tool call, append verified results to JSON:

```json
{
  "execution_result": {
    "submitted": true,
    "exchange_order_id": "verified-id",
    "client_order_id": "decision-derived-id",
    "status": "PARTIALLY_FILLED",
    "filled_quantity": 0.004,
    "average_fill_price": 116905.0,
    "fees_paid": 0.23,
    "protective_stop_verified": true,
    "reconciliation_time_utc": "2026-07-27T12:00:02Z"
  }
}
```

Never claim an order was placed, filled, stopped, canceled, or protected without a connector response that verifies it.
