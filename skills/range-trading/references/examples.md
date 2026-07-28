# Output Examples

Use these examples for structure and decision-state wording. Values are illustrative and not market data.

## Example 1 — Conditional long setup

```markdown
## Range-trading assessment — XYZ | Example Exchange | 1h
**As of:** 2026-07-28T12:00:00Z  
**Last finalized candle:** 2026-07-28T11:00:00Z  
**Verdict:** Conditional setup  
**Setup quality:** Medium

### Data quality
- Source/input: user-provided finalized OHLCV
- Finalized bars used: 180
- Missing or uncertain inputs: slippage and funding

### Regime
- ADX: 18.6, flat
- MA slope: 0.06 ATR per bar, flat
- Efficiency/structure: ER20 0.24 with horizontal swing clusters
- Volatility state: stable; ATR ratio 1.08
- Conclusion: range-like

### Range
- Support zone: 99.40–99.70 (3 independent rejection events)
- Resistance zone: 104.80–105.10 (2 independent rejection events)
- Width: 5.40 | 5.3% | 4.2 ATR
- Containment and alternation: 91% of closes inside; 3 side-to-side alternations

### Current setup
- Final-close location: near support
- Closed-bar confirmation: support touched, but the latest rejection candle is not yet final
- Allowed action: wait for long trigger

### Trade plan
- Trigger: finalized 1h close back above 99.70 with RSI turning upward
- Entry zone: 99.70–100.00
- Invalidation: finalized close below the structural stop zone
- Stop: 99.05, outside support by 0.35 ATR plus execution buffer
- Target 1: 102.20
- Target 2: 104.55
- Gross R:R: 3.2
- Net R:R: not computable
- Cost assumptions: fees known; slippage and funding missing
- Position size: formula only; account risk budget not supplied

### Breakout risk
- Classification: Moderate
- Evidence: two recent support tests occurred close together

### Assumptions and limitations
The range is validated, but there is no finalized entry trigger. This is technical analysis, not a guarantee.
```

## Example 2 — No trade in a trend

```markdown
## Range-trading assessment — ABC | Example Venue | 4h
**As of:** 2026-07-28T12:00:00Z  
**Last finalized candle:** 2026-07-28T08:00:00Z  
**Verdict:** No trade  
**Setup quality:** Low

### Data quality
- Source/input: finalized OHLCV
- Finalized bars used: 220
- Missing or uncertain inputs: none material to the regime decision

### Regime
- ADX: 31.4 and rising
- MA slope: 0.27 ATR per bar upward
- Efficiency/structure: ER20 0.62 with higher highs and higher lows
- Volatility state: expanding; ATR ratio 1.58
- Conclusion: trending

### Range
- Support zone: not validated
- Resistance zone: not validated
- Width: not applicable
- Containment and alternation: absent

### Current setup
- Final-close location: outside any defensible horizontal band
- Closed-bar confirmation: not applicable
- Allowed action: no range trade

### Trade plan
- Trigger: reassess only after ADX and slope cool and two independently tested horizontal zones form
- Entry zone: not provided
- Invalidation: not applicable
- Stop: not provided
- Target 1: not provided
- Target 2: not provided
- Gross R:R: not applicable
- Net R:R: not applicable
- Cost assumptions: not applicable
- Position size: not applicable

### Breakout risk
- Classification: Confirmed breakout
- Evidence: two finalized closes beyond the prior congestion with volatility expansion

### Assumptions and limitations
Range trading is inappropriate under the observed trend. No signal is manufactured.
```
