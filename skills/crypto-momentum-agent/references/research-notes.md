# Design Rationale and Limitations

Load this file only when the user asks why the skill is structured this way, requests evidence, or wants to tune/backtest the rules.

## Momentum is a hypothesis, not a guarantee

Published research has documented both time-series and cross-sectional momentum effects in cryptocurrency returns. This supports testing momentum as a strategy family, not treating any specific RSI, volume, or channel threshold as universally valid.

The bundled defaults are operational starting points for paper trading. Validate them separately by venue, product, timeframe, fee tier, and period.

## Crypto data is venue-specific

Candles, volume, spreads, funding, and liquidation mechanics differ by exchange and product. Some exchange APIs explicitly distinguish completed candles from still-updating candles. Preserve and enforce that completion state.

Volume on one venue measures activity on that venue. It is not proof of market-wide demand. Reported crypto volume can also be affected by data-quality and wash-trading problems, especially on weak venues. This is why the skill combines venue identity, quote turnover, spread, depth, listing age, and execution quality rather than treating one volume spike as decisive.

## Perpetual prices have different roles

A perpetual may expose last/traded price, index price, and mark price. Exchanges can use mark price in liquidation and stop systems, while actual execution occurs against the order book. Keep those fields separate and consume the venue's current instrument metadata because funding intervals and formulas can change.

## Why the skill separates signal and fill

A close-confirmed signal is known only after the candle becomes final. Filling the same strategy at that already-known close introduces optimistic timing unless a valid pre-close order model is used. The skill therefore estimates entry from the first executable quote after signal availability or from the next bar in a historical simulation.

## Why risk is calculated before output

A stop level alone does not define account risk. Quantity, contract multiplier, fees, slippage, funding, gap risk, and correlated exposure determine the monetary loss. The skill sizes from a risk budget and rejects candidates that cannot be expressed within venue increments or policy limits.

## Backtest requirements

When testing or tuning:

- preserve point-in-time symbol availability and delistings;
- exclude the signal bar from prior-channel and volume lookbacks;
- model next-executable or next-bar fills rather than same-close fills;
- include maker/taker fees, spread, slippage, funding, and liquidation rules;
- model missing bars, outages, rejected orders, partial fills, and stop gaps;
- use time-based train, validation, and test splits;
- keep a final untouched out-of-sample period;
- evaluate parameter neighborhoods, not only the single best point;
- report turnover, exposure, drawdown, tail losses, and capacity, not only return or Sharpe ratio;
- do not deploy a parameter because it worked on one symbol or one bull market.

## Suggested primary references

- Liu and Tsyvinski, "Risks and Returns of Cryptocurrency," NBER Working Paper 24877; published in Review of Financial Studies.
- Liu, Tsyvinski, and Wu, "Common Risk Factors in Cryptocurrency," NBER Working Paper 25882; published in Journal of Finance.
- Cong, Li, Tang, and Yang, "Crypto Wash Trading," NBER Working Paper 30783; published in Management Science.
- Official venue API documentation for candle completion, mark/index prices, funding, instrument increments, order behavior, rate limits, and liquidation fields.

These references justify the design categories. They do not validate the exact bundled default parameters.
