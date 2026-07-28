# Strategy Specifications

Use one strategy per decision. Keep parameters in a versioned configuration and report them in output. Defaults below are starting profiles for testing, not claims of universal optimality.

## Shared definitions

Calculate all values from final candles only.

### Indicators

- `EMA20`: exponential moving average of close, 20 bars.
- `EMA50`: exponential moving average of close, 50 bars.
- `RSI14`: Wilder RSI, 14 bars.
- `ATR14`: Wilder ATR, 14 bars.
- `ADX14`: Wilder ADX, 14 bars, optional.
- `MACD_12_26_9`: diagnostic only unless a strategy configuration explicitly promotes it to a rule.

Require a consistent indicator implementation between live, paper, and backtest environments.

### Directional regime

Long regime:

```text
close[t] > EMA50[t]
EMA50[t] > EMA50[t-5]
```

Short regime:

```text
close[t] < EMA50[t]
EMA50[t] < EMA50[t-5]
```

Do not use ADX to infer direction. When used, treat `ADX14 > adx_min` and rising as an optional strength gate.

### Relative volume

Prefer quote volume:

```text
RVOL20[t] = quote_volume[t] / median(quote_volume[t-20:t])
```

Exclude the current bar from the denominator. Set `volume_gate_enabled=false` when the venue's volume is not representative or the feed is unreliable. Use a configurable threshold such as 1.2 only as a tested profile, not as a universal truth.

### Extension filter

Avoid entering a move already far from its local trend:

```text
extension_atr = abs(close[t] - EMA20[t]) / ATR14[t]
```

Use a configurable `max_extension_atr`, commonly tested between 2.0 and 3.0. Return `WATCH` rather than chase when exceeded.

### Causal prior levels

Always exclude candle `t`:

```text
prior_high_L = max(high[t-L:t])
prior_low_L  = min(low[t-L:t])
```

Do not use a swing that requires future candles until those future candles have closed and the swing is confirmed.

## 1. Breakout time-series momentum

Use for directional expansion after a prior range or consolidation.

### Default test profile

```text
signal timeframe: 4h
regime timeframe: 1d or 4h
breakout lookback L: 20 bars
RSI period: 14
long RSI minimum: 55
short RSI maximum: 45
ATR stop multiple: 2.0
chandelier trail multiple: 3.0
fade EMA: 20
volume gate: optional
RVOL threshold when enabled: 1.2
max extension: 2.5 ATR
```

For a daily, slower profile, test `L=55` and report that it is a long-horizon trend/momentum variant rather than a short tactical trade.

### Long setup and trigger

Require:

```text
regime = UPTREND
close[t] > prior_high_L
RSI14[t] > rsi_long_min
extension_atr <= max_extension_atr
```

When enabled, also require:

```text
RVOL20[t] >= rvol_min
```

Optional ADX gate:

```text
ADX14[t] >= adx_min
ADX14[t] > ADX14[t-1]
```

### Short setup and trigger

Permit only for an authorized margin or perpetual product. Require:

```text
regime = DOWNTREND
close[t] < prior_low_L
RSI14[t] < rsi_short_max
extension_atr <= max_extension_atr
```

Apply the volume and optional ADX gates symmetrically.

### Entry

Set the signal at finalization of candle `t`. Enter at the first eligible quote after finalization. Use a price-protected marketable limit or venue-native stop entry. Cancel when:

- the order remains unfilled beyond the configured timeout
- price moves beyond the maximum chase distance
- spread or slippage exceeds limits
- the trigger is invalidated before fill

### Initial stop

Default:

```text
long_stop = planned_entry - 2.0 * ATR14[t]
short_stop = planned_entry + 2.0 * ATR14[t]
```

Optionally compare with the breakout structure. If the structurally valid stop is wider than the maximum permitted risk distance, reject the trade rather than forcing a tighter arbitrary stop.

### Management and fade exit

Ratchet a Chandelier-style trail after entry:

```text
long_trail = highest_close_since_entry - 3.0 * current_ATR14
short_trail = lowest_close_since_entry + 3.0 * current_ATR14
```

Never loosen it.

Use this default final-candle momentum-fade exit:

```text
long: close[t] < EMA20[t] AND RSI14[t] < 50
short: close[t] > EMA20[t] AND RSI14[t] > 50
```

Execute on the next eligible quote. Keep the protective stop active until the exit fill is confirmed.

## 2. Pullback continuation momentum

Use for an established trend that retraces toward a local trend reference and then resumes. Treat the pullback as a setup, not the entry itself.

### Default test profile

```text
signal timeframe: 1h or 4h
regime timeframe: 4h or 1d
pullback reference: EMA20
trend reference: EMA50
long RSI setup zone: 40 to 50
short RSI setup zone: 50 to 60
confirmation level: previous final candle high or low
swing window: 5 bars
stop buffer: 0.25 ATR
trail: 2.5 ATR or EMA20 close rule
max pullback depth: configurable
```

### Long setup

Require:

```text
regime = UPTREND
low[t] <= EMA20[t] + touch_tolerance
RSI14[t] in [40, 50]
close[t] above the configured maximum pullback-depth invalidation
```

Do not enter merely because RSI is 40 to 45. Wait for resumed momentum.

### Long trigger

On a later final candle `u`, require:

```text
close[u] > high[u-1]
RSI14[u] > 50
close[u] > EMA20[u]
```

Optionally require positive quote-volume expansion relative to the immediately preceding pullback bars. Do not require a subjectively named candlestick pattern unless its OHLC definition is written in configuration.

### Short setup and trigger

Mirror the rules in a confirmed downtrend:

```text
high[t] >= EMA20[t] - touch_tolerance
RSI14[t] in [50, 60]
close[u] < low[u-1]
RSI14[u] < 50
close[u] < EMA20[u]
```

Permit only when shorting is supported and authorized.

### Stop

Use a causal local structure:

```text
long_stop = min(low of the previous 5 final bars) - 0.25 * ATR14
short_stop = max(high of the previous 5 final bars) + 0.25 * ATR14
```

Reject the trade if the stop distance breaches the configured maximum or leaves insufficient reward after costs.

### Exit

Select exactly one primary trailing method in configuration:

- `atr_trail`: 2.5 ATR from the best close since entry
- `ema20_close`: final close through EMA20 plus RSI crossing the neutral level

Do not take partial profit automatically unless a tested policy explicitly defines size, level, and the remaining-position trail.

## 3. Cross-sectional rotation

Use to rank a liquid point-in-time universe on one venue and one comparable product set.

### Universe rules

Require:

- same quote or collateral basis where practical
- same product type
- sufficient listing age and history
- current tradable status
- minimum quote volume and depth
- spread within limits
- no leveraged tokens, rebasing tokens, or products whose payoff is not comparable unless explicitly allowed
- point-in-time membership for backtests

Do not compare spot returns directly with leveraged-perpetual PnL without normalizing the construction.

### Default test profile

```text
rebalance schedule: fixed UTC timestamp
lookback: 30 calendar days, translated to bars
ranking metric: trailing return
position weighting: inverse realized volatility with caps
selection: top N or top percentile
short bottom group: disabled by default
minimum holding period: one rebalance interval
```

Calculate trailing return from final prices available before the rebalance decision. Exclude the rebalance candle if it was not final before decision time.

Use raw trailing return for ranking unless a versioned configuration specifies a different score. Apply inverse-volatility weighting separately so that ranking and sizing remain understandable.

### Portfolio construction

1. Filter the point-in-time universe for data quality and liquidity.
2. Rank eligible symbols by trailing return.
3. Select the configured top group.
4. Calculate realized volatility over a declared window.
5. Assign inverse-volatility weights.
6. Cap each symbol and correlated cluster.
7. Normalize weights after caps.
8. Estimate turnover, spread, fees, and slippage.
9. Skip changes whose expected benefit is smaller than estimated trading cost according to the configured buffer.

Short the bottom group only on perpetuals or an authorized margin product and only when the risk policy explicitly enables it. Account for borrow or funding cost and momentum-crash risk.

### Exit and rebalance

Exit at the next scheduled rebalance when a symbol leaves the selected group, or earlier on:

- protective stop
- symbol suspension or delisting risk
- liquidity failure
- stablecoin or collateral event
- venue or data-integrity failure

Do not use an unscaled fixed percentage gap stop as a universal rule. Size stops and positions using volatility and portfolio risk.

## Signal quality and decision states

Use gates rather than vague confidence language.

- `ENTER_LONG` or `ENTER_SHORT`: every required setup, trigger, data, risk, and execution gate passes.
- `WATCH`: setup exists but the trigger has not closed, the move is overextended, or order conditions are temporarily poor.
- `NO_TRADE`: regime, data, liquidity, costs, or risk gates fail.
- `MANAGE_ONLY`: an open position or uncertain account state requires attention before new entries.

Report optional confirmations separately. Never use optional indicators to override a failed hard gate.
