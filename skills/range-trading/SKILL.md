---
name: range-trading
description: Analyze a market for range-trading conditions and generate concrete buy/sell signals around support and resistance. Use this skill whenever the user wants to range-trade, asks to find or evaluate a trading range, mentions support and resistance levels, asks "is this asset range-bound", wants entry/exit/stop levels for a sideways market, or asks whether a market is suitable for buying-the-dip-and-selling-the-rip within a band. Trigger this even when the user only describes the behavior (e.g. "this thing keeps bouncing between two prices, where do I get in?") without naming the strategy.
---

# Range Trading

Range trading is a short-to-medium-term strategy that profits from price oscillating between a defined support (floor) and resistance (ceiling) in a market that lacks a clear directional trend. The core action is simple: **buy near the bottom of the range, sell near the top**, and repeat as long as the range holds.

This skill helps you (1) decide whether a market is actually range-bound, (2) locate the range boundaries, (3) generate entry, exit, and stop levels, and (4) flag when the range is at risk of breaking.

The single most important judgment is the first one. Range trading only works in **stable, sideways, non-trending markets**. In a strong trend, applying this strategy will repeatedly put you on the wrong side of the move. So the analysis always starts by confirming the absence of a trend before doing anything else.

## When this strategy fits

Range trading is suitable when:

- Price is oscillating between two roughly horizontal levels without breaking beyond them.
- There is no strong directional trend (this is the precondition — see the trend filter below).
- The market is relatively stable, so the boundaries are respected repeatedly.
  It works across forex, stocks, indices, commodities, and crypto. The main difference between instruments is **volatility**, which sets how wide the range is and how much risk each trade carries. Higher-volatility instruments (e.g. Bitcoin) mean wider ranges, bigger potential returns, and bigger risk. In forex, currency crosses that exclude the USD (e.g. EUR/CHF) tend to trend weakly and range more often, making them natural candidates. Indices like the S&P 500 trend upward over the long run but still offer intraday ranges.

Do **not** range-trade when the market is trending strongly or is highly volatile with expanding boundaries — in those conditions the price is likely to break out of the range, and a trend-following or momentum approach is more appropriate.

## Analysis workflow

Follow these steps in order. Do not skip the trend filter — it is what protects you from applying this strategy in the wrong conditions.

### Step 1 — Confirm the market is range-bound (trend filter)

Before looking for a range, confirm there is no dominant trend. Use these checks together; agreement across them increases confidence:

- **ADX (Average Directional Index):** A reading **below ~25** indicates a weak or absent trend, which favors range trading. A reading climbing above ~25–30 signals a trend is taking over — range trading becomes unsafe.
- **Moving average behavior:** If a medium-term moving average (e.g. 50-period) is roughly **flat / horizontal**, that supports a range. A steeply sloped MA indicates a trend.
- **Visual structure:** Price should be making roughly **equal highs and equal lows**, not a staircase of higher highs / higher lows (uptrend) or lower highs / lower lows (downtrend).
  If the trend filter says "trending," stop here and tell the user range trading is not appropriate right now, and why. Suggest considering a trend-following approach instead.

### Step 2 — Define the range boundaries

Identify the **support** (lower boundary, where price repeatedly stops falling and bounces up) and **resistance** (upper boundary, where price repeatedly stops rising and turns down).

Quality criteria for a tradeable range:

- **Multiple touches:** Each boundary should have been tested at least **2–3 times**. A single touch is not a confirmed level. More touches = stronger, more reliable boundary.
- **Sufficient width:** The gap between support and resistance must be wide enough that a trade from one side to the other clears costs (spread/fees) and leaves meaningful profit after accounting for a stop placed outside the range. A very narrow range is not worth trading.
- **Roughly horizontal:** The boundaries should be approximately flat. Sloping boundaries indicate a trend or a channel, which this skill does not target.
  State the support price, the resistance price, and the range width explicitly.

### Step 3 — Confirm entries with oscillators

Don't buy at support or sell at resistance purely because price is there — confirm that the boundary is likely to hold. Use one or more of:

- **RSI (Relative Strength Index):** Near support, look for RSI in **oversold** territory (typically ≤30) and ideally turning up — confirmation of a likely bounce. Near resistance, look for RSI **overbought** (typically ≥70) and turning down — confirmation of a likely rejection.
- **Bollinger Bands:** Price tagging the **lower band** near support supports a long; price tagging the **upper band** near resistance supports a short. In a range, price tends to revert toward the middle band.
- **Bounce confirmation:** A reversal candle or a small turn off the level adds confidence versus catching a falling knife.
  If price reaches a boundary but the oscillator does **not** confirm (e.g. price at support but RSI still falling hard with no oversold reading), treat it as a warning that the boundary may break rather than hold.

### Step 4 — Generate the signal

A complete range-trading signal specifies all of the following:

- **Direction:** Long near support, short near resistance.
- **Entry zone:** A price near the boundary (not a single tick — boundaries are zones).
- **Target (take-profit):** The opposite boundary, or slightly before it to exit ahead of the crowd.
- **Stop-loss:** Placed **outside the range**, beyond the boundary you entered at (see stop-loss rules below).
- **Risk-reward check:** Confirm the distance to target is acceptably larger than the distance to stop. Reject the trade if the ratio is unfavorable.

### Step 5 — Flag breakout risk

Always note conditions that suggest the range is about to break, because a breakout is the primary risk of this strategy:

- ADX rising through ~25–30.
- Price closing decisively beyond a boundary (not just an intrabar wick).
- A surge in volatility or volume at a boundary.
- The oscillator failing to confirm at the boundary (Step 3).
  If breakout risk is elevated, say so and recommend standing aside or tightening risk.

## Stop-loss placement

Correct stops are central to range trading because the whole edge depends on the boundary holding. Apply these principles:

- **Place stops outside the range.** The stop goes beyond the support (for longs) or resistance (for shorts) that defines the range, so a genuine breakout takes you out cleanly. A stop inside the range gets hit by normal oscillation.
- **Leave a buffer for false breakouts.** Price often pokes just past a boundary and snaps back. The stop distance should account for this — too tight and you're whipsawed out of good trades.
- **Size the buffer with volatility.** Use a volatility measure such as ATR (Average True Range) to scale the stop: wider stops in more volatile conditions, tighter stops when volatility is low. ATR gives an objective read on the asset's typical fluctuation.
- **Anchor to structure.** Place the stop just beyond a significant support/resistance level rather than at an arbitrary distance.
- **Respect the risk-reward ratio.** The stop distance, combined with the target (opposite boundary), must keep the trade's risk-reward favorable. If a volatility-appropriate stop makes the risk-reward unattractive, the trade isn't worth taking.
- **Stay adaptive.** Boundaries and volatility evolve. Revisit stops as new support/resistance forms or as volatility shifts within the range.

## Output format

When generating a range-trading analysis, present it in this structure:

```
## Range Trading Analysis — [ASSET] [timeframe]

**Trend filter:** [Range-bound / Trending] — [ADX value, MA slope, structure notes]
→ [If trending: state range trading is NOT appropriate and stop here.]

**Range boundaries:**
- Support: [price] ([N] touches)
- Resistance: [price] ([N] touches)
- Range width: [absolute + % terms]

**Current setup:**
- Price location in range: [near support / mid / near resistance]
- Oscillator confirmation: [RSI value, Bollinger position, etc.]

**Signal:** [Long near support / Short near resistance / No trade — wait]
- Entry zone: [price range]
- Target: [opposite boundary]
- Stop-loss: [price, outside the range, with ATR-based rationale]
- Risk-reward: [ratio]

**Breakout risk:** [Low / Elevated] — [reasons]

**Notes:** [caveats, choppiness warning, market-condition reminders]
```

## Key reminders

- **No trade is a valid output.** If the market is trending, the range is unconfirmed (too few touches), the range is too narrow, or breakout risk is high, the correct recommendation is to stand aside. Don't manufacture a signal where conditions don't support one.
- **Limited profit, capped by the range.** Range trading deliberately targets small, repeatable oscillations. Don't chase big moves with it — if you want to capture a breakout, that's a different strategy.
- **Choppy markets cause false signals.** Range-bound conditions can produce erratic whipsaws inside the boundaries. Acknowledge this risk rather than presenting signals as certainties.
- **This is analysis, not financial advice.** Present levels, signals, and risks as structured technical analysis for the user to act on at their own discretion. Don't guarantee profitability.
