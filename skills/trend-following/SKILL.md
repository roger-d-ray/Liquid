---
name: trend-following
description: Analyze a market for a prevailing trend and generate concrete entry, exit, and stop signals that ride the trend in its direction. Use this skill whenever the user wants to trend-trade or trend-follow, asks "is this asset trending", wants to identify an uptrend or downtrend, mentions moving average crossovers (golden cross / death cross), 50/200 EMA, trend lines, or asks where to get in on a move and how long to hold it. Trigger this even when the user only describes the behavior (e.g. "this keeps making higher highs, how do I get on board?") without naming the strategy. This is the opposite regime to range-trading: use this when the market has a clear direction, not when it's stuck in a band.
---

# Trend Following

Trend following (also called trend trading) identifies the prevailing direction of a market and trades **in that direction for as long as the trend lasts**. The guiding mantra is "the trend is your friend." When an uptrend is in place you take long positions to profit from further appreciation; in a downtrend you take short positions to profit from falling prices.

This skill helps you (1) confirm a trend exists and in which direction, (2) confirm it with a momentum indicator, (3) generate an entry, and (4) define stop-loss and exit rules to ride the trend and get out when it turns.

The core idea is the mirror image of range trading. Range trading needs a flat, boundaried market; trend following needs a **clear directional move**. The first job of the analysis is therefore to establish that a genuine trend — not noise or a temporary fluctuation — is present. Trend following deliberately waits for confirmation before acting, accepting slightly later entries in exchange for filtering out false signals.

## When this strategy fits

Trend following is suitable when:

- Price shows a sustained directional move, up or down.
- There's an identifiable structure of higher highs / higher lows (uptrend) or lower highs / lower lows (downtrend).
- Moving averages are sloped and aligned with price on one side.
  Markets move in three directions — up, down, or sideways — and trend following profits from two of them (up and down). It does **not** work in sideways/range-bound markets, where moving-average crossovers whipsaw and produce false signals; in that regime use a range-trading approach instead.

It applies across stocks, forex, commodities, indices, and crypto, though some markets trend more reliably than others. It suits multiple horizons depending on which trend type you're trading.

## Trend types (match the trend to your horizon)

Identify which kind of trend you're trading, because it sets the expected duration and the timeframe to analyze:

- **Secular** — years to decades; structural/demographic drivers.
- **Primary** — months to a few years; business cycle, major political/economic events.
- **Secondary** — weeks to a few months; shifts in sentiment or technical factors.
- **Intermediate** — days to a few weeks; supply/demand shifts, volatility changes.
- **Minor** — a few days; the focus of day traders and swing traders, driven by news and short-term activity.
  Make sure the timeframe you analyze matches the trend the user wants to trade.

## The core method (3 steps)

This is the operational workflow. Apply the steps in order.

### Step 1 — Identify the trend with a moving-average crossover

Use the **50 EMA and 200 EMA** to establish direction:

- **Uptrend:** price is trading **above** both EMAs, and the 50 EMA is above the 200 EMA. The bullish crossover where the 50 crosses **above** the 200 is the **Golden Cross** — a buy signal indicating a potential uptrend. In a confirmed uptrend, look only for **BUY** opportunities; bearish moves are treated as corrections within the larger trend.
- **Downtrend:** price is trading **below** both EMAs, and the 50 EMA is below the 200 EMA. The bearish crossover where the 50 crosses **below** the 200 is the **Death Cross** — a sell signal indicating a potential downtrend. In a confirmed downtrend, look only for **SELL** (short) opportunities.
  The crossover gives the direction; price's position relative to the two EMAs confirms it. If price is whipsawing across the EMAs with no clean separation, there is no usable trend — say so and stand aside.

Trend lines can supplement this: a line connecting **two or more** swing points (rising lows for an uptrend, falling highs for a downtrend) outlines the trend's direction and slope. A bullish chart pattern (e.g. a double bottom) forming near a rising trend line strengthens an entry case.

### Step 2 — Confirm the trend with a momentum indicator

Add **one** momentum indicator to confirm and to time entries. Use MACD (the document's primary choice), or alternatively RSI or the Stochastic Oscillator — but **only one of these at a time**. RSI, Stochastic, and MACD overlap heavily in function; stacking them gives no real edge and clutters the read.

- **MACD:** when the MACD line crosses **above** the signal line it signals bullish momentum (supports buys in an uptrend); a cross **below** the signal line signals bearish momentum (supports sells in a downtrend). The histogram shows momentum strength.
- **RSI (0–100):** above 70 = overbought (possible reversal/exhaustion); below 30 = oversold (possible upward correction). Reads the speed and magnitude of price changes.
- **Stochastic Oscillator (0–100):** compares closing price to its recent range; used to spot overbought/oversold conditions and potential reversals.
  The key combination is **moving averages + momentum**: the EMAs define the trend and filter out the momentum indicator's false signals, while the momentum indicator helps catch additional entries while riding the trend. A MACD buy signal that occurs against the EMA trend (e.g. a MACD buy while price is below both EMAs in a downtrend) is treated as a false signal and ignored — the trend filter overrides it. This filtering is the whole point of using them together.

Continuation candlestick patterns (e.g. Three White Soldiers in an uptrend, Three Black Crows in a downtrend) can serve as additional confirmation that the prevailing trend will continue.

### Step 3 — Apply risk management (stop-loss and exit)

No trend lasts forever, so define the stop and exit before entering.

**Stop-loss:**

- In an **uptrend**, place the stop **below the recent swing low**. (In a downtrend, mirror it: above the recent swing high.) This keeps you in the trade through normal pullbacks but exits you if the trend structure breaks.
  **Exit (trend-turn rule):**
- For a **BUY/long** position, exit when price **closes below** one of the moving averages for **at least two candlesticks**. The two-candle confirmation avoids reacting to a single noisy close.
- For a **SELL/short** position, exit when price **closes above** one of the EMAs for **two or more candlesticks**.
  This close-based, multi-candle exit is what lets you ride the trend as long as it persists while still getting out when it genuinely reverses.

## Output format

When generating a trend-following analysis, present it in this structure:

```
## Trend-Following Analysis — [ASSET] [timeframe]

**Trend direction:** [Uptrend / Downtrend / No clear trend]
- 50 EMA vs 200 EMA: [above/below — Golden Cross / Death Cross / none]
- Price vs EMAs: [above both / below both / tangled]
- Structure: [higher highs & higher lows / lower highs & lower lows / choppy]
→ [If no clear trend: state trend following is NOT appropriate here and stop.]

**Momentum confirmation:** [MACD / RSI / Stochastic — value and read]
- Aligned with trend? [yes / no — if no, signal is filtered out]

**Signal:** [Buy/long with the uptrend / Sell/short with the downtrend / No trade — wait]
- Entry: [price / condition]

**Risk management:**
- Stop-loss: [below recent swing low (long) / above recent swing high (short) — price]
- Exit rule: [close beyond an EMA for 2+ candles]

**Trend type / horizon:** [minor / intermediate / primary, etc.]

**Notes:** [noise/false-signal warnings, drawdown risk, correction vs reversal]
```

## Key reminders

- **The trend filter overrides the oscillator.** Never take a momentum signal that fights the EMA trend — that's exactly the false signal the moving averages are there to filter out.
- **No trade is a valid output.** If the EMAs are tangled, price is whipsawing, or there's no clean directional structure, the market is not trending — recommend standing aside rather than forcing a trade. This is the range-trading regime, not trend following.
- **Use one momentum indicator, not three.** RSI, Stochastic, and MACD are functionally similar; combining them adds clutter, not edge.
- **Accept delayed entries.** Trend following waits for confirmation, so entries (and exits) come slightly late by design. The tradeoff is fewer false signals. Don't try to pre-empt the confirmation.
- **Distinguish corrections from reversals.** In a strong trend, counter-trend moves are often just corrections. The 2-candle-close-beyond-EMA exit rule is what separates a real reversal from noise — don't bail on the first red candle.
- **This is analysis, not financial advice.** Present direction, signals, and risk levels as structured technical analysis for the user to act on at their discretion. Don't promise profits; trend following has real drawdown risk and false signals.
