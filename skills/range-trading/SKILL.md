---
name: range-trading
description: Analyze finalized OHLCV data to determine whether an asset is trading inside a horizontal range, identify support and resistance zones, assess regime and breakout risk, and produce conditional or confirmed mean-reversion trade plans with entries, targets, stops, costs, net risk-reward, and optional position sizing. Use when the user asks whether a market is range-bound, wants range levels or repeated support/resistance bounces, asks where to buy near support or sell, exit, or short near resistance in a sideways market, or describes price as repeatedly bouncing between two levels. Do not use for breakout trading, trend following, arbitrage, long-term valuation, or live order execution.
---

# Range Trading

## Objective

Determine whether a stable horizontal range exists and return one of four states:

1. **Confirmed setup** — every mandatory gate passes on finalized data.
2. **Conditional setup** — the range is valid, but price location or closed-bar confirmation is incomplete.
3. **No trade** — one or more mandatory gates fail.
4. **Insufficient data** — freshness, history, costs, or required fields are inadequate for a defensible conclusion.

Prefer no trade over a weak or invented signal.

## Non-negotiable rules

- Base indicators, pivots, range boundaries, confirmations, and breakout decisions only on candles with `is_final=true` and `available_at <= analysis_time`.
- Treat a live or non-final quote only as execution context. Never let it create, confirm, or invalidate a signal by itself.
- Never invent prices, indicator values, fees, volume, account size, short availability, or data freshness.
- Treat support and resistance as zones, not exact ticks.
- Distinguish **selling an existing long** from **opening a short**. Propose a new short only when shorting is allowed or explicitly requested.
- Never widen an initial stop after entry. Tighten it only under a predeclared rule.
- Calculate trade economics after spread, fees, slippage, funding, and borrow costs when those inputs are available.
- Label every numeric threshold as a default heuristic unless the user supplies a preferred rule set.
- Do not execute orders. Provide technical analysis or a paper-trade plan only.

## Load supporting guidance

- Read `references/data-contract.md` when data fields, freshness, timeframes, costs, or instrument constraints are unclear.
- Read `references/methodology.md` before constructing zones, classifying the regime, or calculating entry, stop, target, and breakout conditions.
- Read `references/examples.md` when output style or decision-state wording is unclear.
- When raw OHLCV is available as a local CSV or JSON file, run `scripts/compute_range_metrics.py` and use its output as diagnostics. Do not treat the script output as a trade recommendation.

## Workflow

Follow the sequence below. Do not jump directly from a visible support level to a signal.

### 1. Establish scope and constraints

Identify or obtain:

- asset, venue, and instrument type;
- primary analysis timeframe and `analysis_time`;
- finalized OHLCV history;
- allowed directions: long-only, exit-only, or long/short;
- tick size and estimated trading costs when available;
- optional higher-timeframe context;
- optional account risk budget for position sizing.

If essential data is missing, return **Insufficient data** and specify exactly what is missing. Do not silently substitute generic market data.

### 2. Validate data quality and finality

- Sort bars chronologically, remove or resolve duplicate timestamps, and keep one consistent timezone.
- Require valid OHLC values and enough finalized history for the selected indicators and range lookback.
- Use at least 80 finalized bars for a limited assessment; prefer 150–200 or more.
- Verify that precomputed indicators use the same finalized bars, timeframe, and disclosed periods.
- State the last finalized candle timestamp and the data source or input origin.

If the data fails these checks, stop with **Insufficient data**.

### 3. Classify the market regime

Evaluate the primary timeframe with multiple independent measures:

- ADX level and direction;
- normalized medium-term moving-average slope;
- directional efficiency ratio or equivalent price-path measure;
- volatility expansion or contraction;
- swing structure: horizontal highs/lows versus directional stair-stepping.

Use the defaults in `references/methodology.md`. Do not classify a market as range-bound from low ADX alone.

Apply these rules:

- If at least two independent measures support a range and none is strongly adverse, continue.
- If trend evidence is strong, return **No trade** for range trading.
- If the evidence is mixed, continue only as a **Conditional setup** and lower confidence.
- Use a higher timeframe as context, not as a hidden veto. In a strong higher-timeframe uptrend, favor long bounces and downgrade shorts; reverse this logic in a downtrend.

### 4. Construct and validate the range

Build support and resistance zones from finalized pivot clusters that existed before the prospective signal bar.

Require:

- at least two independent rejection events at each boundary; prefer three;
- meaningful time separation between tests;
- approximately horizontal zones within an ATR- or percentage-based tolerance;
- evidence of alternating movement between the lower and upper parts of the band;
- sufficient containment of closes inside the band;
- enough width to support a volatility-aware stop and positive net reward-to-risk.

Do not count repeated taps without a meaningful move away as separate strong touches. Treat rapid retesting, shrinking rebounds, or one-sided pressure as breakout risk.

If the boundaries are sloped, expanding, or converging, identify the structure as a channel, broadening formation, or compression rather than forcing a horizontal range.

### 5. Evaluate price location and closed-bar confirmation

Normalize price location inside the band:

`location = (price - support_center) / (resistance_center - support_center)`

Use the final close for decision-making.

- Near support: consider a long only when price enters the support zone and a finalized candle rejects or reclaims it.
- Near resistance: consider an exit or short only when price enters the resistance zone and a finalized candle rejects it.
- Mid-range: return **No trade — poor location** unless the user explicitly requests management of an existing position.

Require location plus closed-bar rejection and at least one supporting confirmation, such as:

- RSI turning away from a local extreme or showing divergence;
- Bollinger-band rejection with re-entry inside the band;
- reversal-body or wick structure relative to ATR;
- declining directional pressure;
- meaningful venue volume, when reliable.

Do not require RSI to reach exactly 30 or 70. Do not use volume when its source is not meaningful for the instrument.

### 6. Build the trade plan and test economics

For a valid setup, specify:

- direction and whether it means entry, short entry, or exit of an existing position;
- trigger condition on a finalized candle;
- entry zone, rounded only to a known tick size;
- structural invalidation level;
- stop beyond the outer edge of the boundary zone plus a volatility and execution buffer;
- first target near the range midpoint when useful;
- final target before the opposite boundary zone;
- gross and net reward-to-risk;
- assumptions for spread, fees, slippage, funding, and borrow;
- optional position size only when risk budget and contract details are known.

Reject the trade when net reward is non-positive or net reward-to-risk is below the user threshold. Use 1.5 as a default minimum only when the user has not supplied one.

### 7. Assess breakout and failure risk

Evaluate:

- finalized closes outside the outer zone;
- distance of the close beyond the boundary in ATR terms;
- ADX and moving-average slope acceleration;
- ATR or Bollinger-bandwidth expansion;
- reliable volume expansion;
- repeated pressure tests and shrinking bounce amplitude;
- scheduled event risk, only when a trustworthy calendar is available.

Classify breakout risk as **Low**, **Moderate**, **High**, or **Confirmed breakout**. Do not fade a confirmed breakout. Treat a close outside followed by a finalized close back inside as a possible failed breakout, but require a new re-entry confirmation before considering a trade.

### 8. Return the result

Use the exact section order below. Keep the verdict concise, show the evidence, and make missing assumptions explicit.

```markdown
## Range-trading assessment — [ASSET] | [VENUE] | [TIMEFRAME]
**As of:** [analysis time]  
**Last finalized candle:** [timestamp]  
**Verdict:** [Confirmed setup / Conditional setup / No trade / Insufficient data]  
**Setup quality:** [High / Medium / Low / Not assessable]

### Data quality
- Source/input: [source]
- Finalized bars used: [N]
- Missing or uncertain inputs: [items or none]

### Regime
- ADX: [value and direction]
- MA slope: [normalized value and interpretation]
- Efficiency/structure: [value and interpretation]
- Volatility state: [stable / contracting / expanding]
- Conclusion: [range-like / mixed / trending]

### Range
- Support zone: [low–high] ([N] independent rejection events)
- Resistance zone: [low–high] ([N] independent rejection events)
- Width: [absolute] | [%] | [ATR multiple]
- Containment and alternation: [evidence]

### Current setup
- Final-close location: [near support / mid-range / near resistance / outside]
- Closed-bar confirmation: [evidence or missing condition]
- Allowed action: [long / exit long / short / wait]

### Trade plan
- Trigger: [finalized-candle condition]
- Entry zone: [range]
- Invalidation: [condition]
- Stop: [price and buffer rationale]
- Target 1: [price or not used]
- Target 2: [price]
- Gross R:R: [ratio]
- Net R:R: [ratio or not computable]
- Cost assumptions: [details]
- Position size: [quantity and risk, formula only, or not computable]

### Breakout risk
- Classification: [Low / Moderate / High / Confirmed breakout]
- Evidence: [reasons]

### Assumptions and limitations
[Short factual caveats. State that this is technical analysis, not a guarantee.]
```

If the verdict is **No trade** or **Insufficient data**, omit fabricated entry levels and state the next objective condition that would justify reassessment.

## Quality controls

Before responding, verify all of the following:

- Every decision value comes from finalized data available by `analysis_time`.
- The range was defined without future bars or the prospective signal bar influencing prior boundaries.
- Support and resistance are zones with independent tests, not arbitrary single prices.
- Regime classification uses more than one indicator.
- The action respects long-only or shorting constraints.
- Stop, targets, costs, and reward-to-risk use the same entry assumption.
- The stop was not placed inside normal range noise and was not widened after entry.
- The output distinguishes confirmed evidence from assumptions and live context.
