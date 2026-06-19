---
name: momentum-trading
description: Analyze a market for momentum and generate concrete buy-strength / sell-weakness signals with entry, stop, trailing, and momentum-fade exit rules. Use this skill whenever the user wants to momentum-trade, asks to "buy strength and sell weakness", asks about breakouts on volume, new 20-day/55-day highs or lows, RSI/MACD/ADX momentum confirmation, rotational or relative-strength ranking, or pullback entries into a trend. Trigger this even when the user only describes the behavior (e.g. "this is breaking out hard on volume, do I chase it?") without naming the strategy. Distinct from trend-following: momentum trades the SPEED of the move on shorter horizons and exits when momentum fades, not when the long trend structurally reverses.
---

# Momentum Trading

Momentum trading means **buying strength and selling weakness**: following price that is already moving, riding the move while it has speed, and exiting when momentum fades. The premise is that strong moves tend to keep moving because new participants pile in (herding), creating a self-feeding loop until momentum dries up. It works on any liquid market and fits both day and swing timeframes.

This skill helps you (1) confirm momentum exists and in which direction, (2) wait for a concrete entry trigger, (3) enter on a finished candle, (4) manage risk with a stop and trailing exit, and (5) exit when momentum fades.

## Momentum vs. trend following (important distinction)

These two skills overlap — both ride price direction — but they are **not the same**, and choosing the right one matters:

- **Trend following** holds for **weeks to months** and exits when the long-term trend structurally reverses (e.g. price closing beyond the 50/200 EMA for multiple candles). It trades the _direction_ of the big trend.
- **Momentum trading** trades the **speed/strength** of a move on **shorter horizons** (can flip after hours or days), uses tighter stops, trades more frequently, and exits the moment momentum _fades_ — even if the larger trend is technically intact.
  If the user wants to ride a major directional trend for the long haul, prefer `trend-following`. If they want to capture a fast, strong move and get out when it loses steam — breakouts, new highs on volume, relative-strength rotation, pullback re-entries — use this skill.

Momentum works best in clear bull or bear phases when price runs. In choppy sideways markets, signals fail more often, so cut position size or wait for stronger confirmation.

## The core method

Think of price as a train already moving: jump on while it has speed, hop off before it slows. Apply these steps in order.

### Step 1 — Spot the move (direction)

Scan for a strong directional move and confirm direction with a moving average. Price **above a rising 50-period average** shows upward momentum (long bias); price below a falling 50-period average shows downward momentum (short bias). No clean directional move = no momentum trade.

### Step 2 — Confirm with momentum indicators

Filter out weak moves. Look for confirmation such as:

- **RSI above 60** (strong upward momentum; for longs, the article's rule is long only when RSI > 55 in an uptrend, and stay flat / cautious above 70 where the move is overextended).
- **MACD line above zero** (and above its signal line) for longs; below for shorts. Exit cue when MACD line crosses below signal.
- **Rising ADX** — confirms the move has real strength behind it.
- **Volume** — a genuine breakout should print elevated volume (the playbook uses ~120% of average, i.e. a clear volume spike). Volume is the key confirmation that demand is real.
  A high-quality signal is **triple alignment**: e.g. price closing above the upper Bollinger Band, RSI around 65, and a volume jump together.

### Step 3 — Wait for the trigger

Don't anticipate — wait for a concrete signal:

- **Buy signal:** price makes a **new 20-day high on rising volume**.
- **Sell signal:** price breaks a **20-day low with heavy selling**.
  (The breakout lookback can be longer for longer-horizon systems — e.g. a 55-day high for the pure breakout strategy below.)

### Step 4 — Enter (don't chase)

Place the order **as the candle closes beyond the trigger**. Let the candle finish — do not chase a late move mid-bar. Entering on the close avoids getting faked out by an intrabar spike that reverses.

### Step 5 — Set the stop-loss

Cap risk immediately:

- **Longs:** stop **below the last minor swing low**.
- **Shorts:** stop **above the last swing high**.
- A volatility-based alternative is an ATR-distance stop (e.g. 1.5–2 ATR), used by several of the strategies below.

### Step 6 — Ride the move (trail)

Let winners run. Trail the stop using a moving average or a fixed ATR distance so profit is protected while the move continues.

### Step 7 — Exit on momentum fade

Close when the speed is gone — this is the defining exit of momentum trading:

- **Long:** RSI dips **under 50**, or MACD crosses down, or a sell signal prints (e.g. close below a 10- or 20-day low).
- **Short:** mirror — RSI recovers / MACD crosses up / a buy signal prints.
  The exit is triggered by **momentum fading**, not by waiting for the entire trend to reverse. That's what separates this from trend following.

## Three ready strategies

Match the variant to the user's horizon and asset universe.

### 1. Pure Breakout Momentum (time-series) — "green light"

- **Entry:** asset closes at a **55-day high with volume confirmation** → buy at the close.
- **Exit:** price closes below a **20-day low**, or MACD flips negative.
- **Hold:** weeks to months (long-term momentum).
- **Best for:** trending futures and major forex pairs. Mechanical and easy to backtest.

### 2. Rotational Momentum (cross-sectional)

- **Method:** rank a universe (e.g. S&P 500, or major forex pairs) by trailing return (e.g. 6-month or 3-month). Go long the top names; ignore or short the bottom. Rebalance on a fixed schedule (e.g. weekly after Friday close, rebalance Monday).
- **Exit:** automatic at next rebalance, or if a name gaps ~7% against the position.
- **Hold:** ~one week (short-term momentum with periodic refresh).
- **Watch:** transaction costs matter — use liquid, tight-spread instruments only. This captures **relative strength between names**, not each name's own trend.

### 3. Pullback Momentum

- **Setup:** uptrend confirmed by a **rising 50-EMA**; **RSI pulls back to ~40–45**; a **bullish engulfing** candle prints; volume ticks up. Buy the dip, not the high — lower-risk entry than a straight breakout.
- **Exit:** trail stop ~2 ATR below price; take partial profit at the prior swing high.
- **Hold:** several days to a few weeks.
- **Caveat:** pullback momentum fails more when market breadth is weak — check breadth (e.g. NYSE advance/decline, or crypto total-market-cap) first.

## Indicator toolbox

Keep it small:

- **Moving averages (20-EMA, 50-EMA, 200-SMA):** trend direction and pullback zones. Price above a rising 50-EMA = buy bias.
- **RSI:** speed of the move. Long only when RSI > 55 in an uptrend; caution above 70.
- **MACD:** momentum shifts. Exit long when MACD line crosses below signal.
- **Stochastic Oscillator:** short-term swings; fade overbought pullbacks within a larger uptrend.
- **Bollinger Bands:** volatility squeeze/breakout. Enter on close above the upper band with a volume spike.
- **Volume:** confirmation. A breakout should print ~120% of average volume.

## Output format

When generating a momentum-trading analysis, present it in this structure:

```
## Momentum Analysis — [ASSET] [timeframe]

**Market phase:** [Clear bull/bear run — momentum favorable / Choppy sideways — reduce size or wait]

**Direction:** [Up / Down / No clean move]
- Price vs rising/falling 50-MA: [above & rising / below & falling / tangled]

**Momentum confirmation:**
- RSI: [value + read]
- MACD: [above/below zero & signal]
- ADX: [rising/falling]
- Volume: [vs average — is the move backed by volume?]
- Triple-alignment present? [yes/no]

**Trigger:** [new 20-day high on volume / 20-day low on heavy selling / none yet — wait]

**Signal:** [Buy strength / Sell weakness / No trade — wait for trigger]
- Strategy variant: [Breakout / Rotational / Pullback]
- Entry: [on candle close beyond trigger — price]
- Stop-loss: [below swing low / above swing high / N×ATR — price]
- Trailing plan: [MA trail / ATR trail]
- Momentum-fade exit: [RSI < 50 / MACD cross down / new opposite signal]

**Notes:** [chase warning, news risk, breadth check for pullbacks, liquidity]
```

## Key reminders

- **Don't chase.** Enter on the candle close beyond the trigger, never mid-bar into a late move. A finished candle is the filter against fakeouts.
- **No trade is a valid output.** In choppy, sideways, low-volume, or low-breadth conditions, momentum signals fail — recommend waiting or smaller size rather than forcing a trade.
- **Volume is non-negotiable for breakouts.** A breakout without a volume spike is a low-quality signal. Demand has to be real.
- **Exit on fade, not on hope.** The whole edge is stepping off before the crowd flips. Honor the momentum-fade exit (RSI<50 / MACD down) even if the move "feels" like it could continue. Reversals are fast.
- **Stick to the stop; never add to losers.** Momentum reversals are abrupt; a blown stop or an averaged-down loser erases the edge.
- **Watch the news.** Big economic releases can flip short-term momentum instantly — factor event risk into entries.
- **Don't over-optimize.** Simple momentum rules often beat heavily filtered ones. If recommending a backtest, change one parameter at a time and keep only settings that survive out-of-sample.
- **This is analysis, not financial advice.** Present signals and levels as structured technical analysis for the user to act on at their discretion. Trading involves real risk; past performance doesn't guarantee future results.
