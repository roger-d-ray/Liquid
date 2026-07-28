# Range-Trading Methodology

These values are robust defaults, not universal constants. Adapt them to the instrument, timeframe, venue, and user constraints, and disclose every material override.

## 1. Regime diagnostics

Use at least three independent categories and avoid a binary decision from one threshold.

### ADX(14)

- `< 20`: supportive of a weak trend.
- `20–25`: neutral or transitional.
- `> 25`: adverse to range trading, especially when rising.
- `> 30` and rising: strong trend or breakout warning.

Interpret direction as well as level. A low but rapidly rising ADX can be more dangerous than a stable reading near 25.

### Normalized moving-average slope

Use a 50-period SMA or EMA unless the user specifies another period.

```text
normalized_slope = abs(MA[t] - MA[t-k]) / (k * ATR14[t])
```

Use `k = 10` by default.

- `<= 0.10 ATR per bar`: supportive of a flat regime.
- `0.10–0.20`: mixed.
- `> 0.20`: adverse.

### Efficiency ratio ER(20)

```text
ER = abs(close[t] - close[t-20]) / sum(abs(close[i] - close[i-1]), i=t-19..t)
```

- `<= 0.30`: range-like path.
- `0.30–0.50`: mixed.
- `> 0.50`: directionally efficient and trend-like.

### Volatility state

Compare current ATR(14) with the median ATR over the prior 50 bars:

```text
ATR_ratio = ATR14[t] / median(ATR14[t-50:t])
```

- `<= 1.25`: stable or ordinary volatility.
- `1.25–1.50`: elevated.
- `> 1.50`: expansion warning.

Also inspect Bollinger bandwidth direction. Rising bandwidth plus directional closes is adverse even when ADX is still lagging.

### Structural check

Classify swings as range-like only when highs and lows cluster horizontally. A staircase of higher highs and higher lows, or lower highs and lower lows, overrides a superficially low ADX.

## 2. Boundary construction

### Pivot identification

Use finalized bars only. A pivot requires bars on both sides, so the latest few candles normally cannot be confirmed pivots.

Use a default pivot window of two bars on each side. Increase it on noisy lower timeframes.

### Zone tolerance

Start with:

```text
zone_tolerance = max(0.25 * ATR14, 0.15% of price)
```

Adjust for tick size, spread, and instrument volatility. Build each zone around the median of a pivot cluster rather than its most extreme wick.

### Independent rejection event

Count a boundary test as independent only when:

1. price enters or slightly exceeds the zone;
2. a finalized candle closes back toward the interior; and
3. price subsequently moves at least `0.75 ATR` away, or at least three bars pass before a new test.

Compress rapid repeated taps into one event. Several taps with progressively smaller reactions indicate pressure and may weaken the level.

### Range validation defaults

Require all of the following unless the user supplies another rule set:

- at least two independent rejection events at support and resistance; prefer three;
- at least two meaningful side-to-side alternations;
- at least 85% of finalized closes inside the outer zones during the selected range window;
- boundary drift no greater than about `0.5 ATR` across the window;
- width of at least `3 ATR`, unless direct cost and stop calculations demonstrate acceptable economics at a narrower width;
- no confirmed breakout under the rules below.

Do not assume that more touches always mean greater strength. Quality, spacing, reaction distance, and one-sided pressure matter more than raw count.

## 3. Price location and confirmation

Define:

```text
location = (final_close - support_center) / (resistance_center - support_center)
```

Default zones of interest:

- `location <= 0.20`: lower-edge area.
- `0.20 < location < 0.80`: generally poor new-entry location.
- `location >= 0.80`: upper-edge area.

Use ATR proximity as a second check. A wide range can place `location=0.20` too far from support for a sensible stop.

### Long confirmation near support

Require:

- price touched or entered the support zone; and
- a finalized candle closed back inside or above the inner edge of the zone; and
- at least one supporting condition:
  - RSI(14) is below roughly 40 and turns upward;
  - bullish RSI divergence is present;
  - price re-enters the Bollinger Band after closing or wicking below it;
  - rejection wick or reversal body is meaningful relative to ATR;
  - directional pressure or negative momentum is weakening.

### Resistance confirmation

Apply the symmetric logic. Interpret the action as exit-long unless opening shorts is allowed.

Do not require exact RSI 30/70 readings. In many stable ranges, useful reversals occur before those extremes; in breakdowns, an oversold reading can persist.

## 4. Entry, stop, target, and costs

### Entry

Use one of two clearly labeled styles:

- **Confirmation entry:** enter near the finalized rejection close or on a small retracement after it.
- **Limit-at-zone entry:** use only when the user accepts lower confirmation and higher false-break risk.

Never mix the entry assumption used for the signal with a more favorable entry used for the R:R calculation.

### Stop and invalidation

Place the structural stop beyond the outer edge of the relevant zone. Start with:

```text
buffer = max(0.35 * ATR14, spread + expected slippage, 2 * tick_size)
```

Use the nearest structurally meaningful value that remains outside normal range noise. If the required stop makes economics unattractive, reject the trade.

After entry:

- never widen the initial stop;
- tighten only according to a declared rule, such as after the midpoint is reached or after a new finalized swing forms;
- distinguish a stop order from the analytical invalidation condition, especially for instruments exposed to gaps.

### Targets

- Optional Target 1: range midpoint or nearby mean, particularly for partial risk reduction.
- Final target: place before the opposite boundary's inner edge, commonly 10–20% of the range width inside that boundary, or by an ATR/tick buffer.

Do not target beyond the range with a mean-reversion setup. A breakout continuation requires a different strategy.

### Net reward-to-risk

Convert all costs into price or account-currency units consistently.

```text
net_reward = abs(target - entry) - round_trip_cost
net_risk   = abs(entry - stop) + round_trip_cost
net_RR     = net_reward / net_risk
```

Reject when `net_reward <= 0`. Use a default minimum `net_RR >= 1.5` only when the user has not provided a threshold.

### Position sizing

Calculate a quantity only when risk budget and contract details are known:

```text
risk_budget = account_equity * risk_percent
per_unit_risk = abs(entry - stop) * contract_multiplier + per_unit_costs
quantity = risk_budget / per_unit_risk
```

Round down to venue lot size. Check that liquidation, margin, gap, funding, and borrow constraints do not invalidate the plan. Otherwise provide the formula without a quantity.

## 5. Breakout and failed-break rules

Treat a breakout as confirmed on finalized bars when either condition holds:

1. two consecutive closes finish outside the outer boundary zone; or
2. one close finishes at least `0.5 ATR` beyond the outer zone and the bar's true range is at least `1.5 ATR`, with supporting volume expansion when reliable.

Classify risk as high before confirmation when several of these appear:

- ADX rises through 25–30;
- normalized MA slope accelerates;
- ATR ratio or Bollinger bandwidth expands;
- multiple pressure tests occur without meaningful retreat;
- bounce amplitude shrinks;
- closes cluster against one boundary;
- reliable volume expands at the boundary;
- a known scheduled catalyst is near.

A failed breakout requires a finalized close back inside the range after an outside close. Do not immediately fade it. Require a new rejection or re-entry confirmation and recalculate the stop and net R:R.

## 6. Decision gates

Return **Confirmed setup** only when every gate passes:

1. Data is final, fresh enough, and sufficient.
2. Primary-timeframe regime is range-like or acceptably mixed.
3. Support and resistance zones are independently validated.
4. Price is at an edge, not mid-range.
5. A finalized rejection and at least one confirmation exist.
6. Direction is permitted by the instrument and user constraints.
7. Net economics meet the threshold, or all missing cost inputs are explicitly disclosed and the verdict remains conditional.
8. Breakout risk is not high and no breakout is confirmed.

Return **Conditional setup** when the range is valid but one non-fatal trigger is pending, such as price approaching a zone or a rejection candle not yet finalized.

Return **No trade** when regime, boundary quality, location, economics, direction constraints, or breakout risk fails.
