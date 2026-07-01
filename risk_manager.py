"""
risk_manager.py
Validates skill proposals before sending Telegram notifications.

Input:  proposal dict/JSON produced by a skill analysis
Output: ValidationResult — approved | adjusted | warnings | rejection_reason

Rules are derived from:
  skills/range-trading/SKILL.md
  skills/trend-following/SKILL.md
  skills/momentum-trading/SKILL.md
  CLAUDE.md (global: confidence >= 0.55, max 1 trade per run)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class Proposal:
    """
    Structured proposal produced by one of the three trading skills.
    Fields not relevant to a strategy can be omitted (default None).
    """
    strategy:   str    # range-trading | trend-following | momentum-trading
    asset:      str    # BTC | ETH | SOL
    signal:     str    # long | short | no_trade
    timeframe:  str
    entry:      float
    target:     float
    stop_loss:  float
    confidence: float
    price:      float

    # Execution sizing
    leverage:   Optional[float] = None   # capped at MAX_LEVERAGE

    # Universal context
    adx:            Optional[float] = None
    rsi:            Optional[float] = None
    ema9:           Optional[float] = None
    ema21:          Optional[float] = None
    ema50:          Optional[float] = None
    ema200:         Optional[float] = None
    atr:            Optional[float] = None
    volume_ratio:   Optional[float] = None   # last_volume / vol_sma20
    macd_histogram: Optional[float] = None
    macd_cross:     Optional[str]   = None   # bullish | bearish | none

    # Range-trading specific
    support:             Optional[float] = None
    resistance:          Optional[float] = None
    support_touches:     Optional[int]   = None
    resistance_touches:  Optional[int]   = None
    breakout_risk:       Optional[str]   = None  # low | elevated

    # Momentum-trading specific
    new_20d_high: Optional[bool] = None
    new_20d_low:  Optional[bool] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ValidationResult:
    approved:         bool
    adjusted:         bool          = False
    confidence:       float         = 0.0
    warnings:         list[str]     = field(default_factory=list)
    rejection_reason: Optional[str] = None
    adjustments:      dict          = field(default_factory=dict)
    final_entry:      float         = 0.0
    final_target:     float         = 0.0
    final_stop:       float         = 0.0
    risk_reward:      float         = 0.0
    original:         dict          = field(default_factory=dict)
    validated:        Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "approved":         self.approved,
            "adjusted":         self.adjusted,
            "confidence":       round(self.confidence, 3),
            "warnings":         self.warnings,
            "rejection_reason": self.rejection_reason,
            "adjustments":      self.adjustments,
            "final_entry":      self.final_entry,
            "final_target":     self.final_target,
            "final_stop":       self.final_stop,
            "risk_reward":      round(self.risk_reward, 2),
            "original":         self.original,
            "validated":        self.validated,
        }


# ─── Thresholds ───────────────────────────────────────────────────────────────

# Global (CLAUDE.md)
MIN_CONFIDENCE = 0.55

# Leverage — intraday aggressive policy (CLAUDE.md). Nothing used to validate
# leverage; this is the hard ceiling. Proposals above it are rejected outright.
MAX_LEVERAGE = 20.0

# Risk-reward — loosened for intraday scalps (shorter horizon, tighter targets).
MIN_RR_HARD = 1.2   # reject below
MIN_RR_WARN = 1.8   # warn below

# ATR-based stop sizing — tightened for intraday (15m/1h) horizon so stops sit
# close to entry and positions resolve fast; wide 4h-style stops would keep a
# 20x position open far too long.
ATR_STOP_MIN_MULT    = 0.5    # stop tighter than N×ATR → adjust
ATR_STOP_ADJUST_MULT = 0.75   # adjusted stop placed at N×ATR from entry

# Range trading
ADX_RANGE_HARD_MAX = 25.0   # above → trending → reject range signal
ADX_RANGE_WARN     = 22.0   # approaching → warn
RANGE_MIN_WIDTH_PCT = 1.5   # % of support price
MIN_SUPPORT_TOUCHES    = 2
MIN_RESISTANCE_TOUCHES = 2
RSI_LONG_REJECT  = 50.0     # long at support: RSI above this → reject
RSI_LONG_WARN    = 40.0     # long at support: RSI above this → warn
RSI_SHORT_REJECT = 50.0     # short at resistance: RSI below this → reject
RSI_SHORT_WARN   = 60.0     # short at resistance: RSI below this → warn

# Trend following
EMA_TANGLE_PCT = 0.005      # 0.5% — ema50/ema200 spread below this → tangled

# Momentum trading
RSI_MOM_LONG_MIN   = 55.0   # long requires RSI above
RSI_MOM_LONG_OVER  = 70.0   # long warn: overextended
RSI_MOM_SHORT_MAX  = 45.0   # short requires RSI below
RSI_MOM_SHORT_OVER = 30.0   # short warn: overextended
VOLUME_BREAKOUT_MIN = 1.2   # vol_ratio required for a valid breakout


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _calc_rr(entry: float, target: float, stop: float) -> float:
    risk   = abs(entry - stop)
    reward = abs(target - entry)
    return round(reward / risk, 2) if risk else 0.0


def _adjusted_stop(entry: float, atr: float, signal: str) -> float:
    mult = ATR_STOP_ADJUST_MULT
    return entry - mult * atr if signal == "long" else entry + mult * atr


# ─── Universal validator ──────────────────────────────────────────────────────

def _validate_universal(p: Proposal, r: ValidationResult) -> bool:
    # 1. Minimum confidence (CLAUDE.md global rule)
    if p.confidence < MIN_CONFIDENCE:
        r.rejection_reason = (
            f"Confidence {p.confidence:.2f} is below the minimum {MIN_CONFIDENCE}. "
            "No alert sent."
        )
        return False

    # 1b. Leverage ceiling (intraday aggressive policy)
    if p.leverage is not None and p.leverage > MAX_LEVERAGE:
        r.rejection_reason = (
            f"Leverage {p.leverage:g}x exceeds the maximum {MAX_LEVERAGE:g}x allowed."
        )
        return False

    # 2. Directional coherence
    if p.signal == "long":
        if p.target <= p.entry:
            r.rejection_reason = "Long: target must be above entry."
            return False
        if p.stop_loss >= p.entry:
            r.rejection_reason = "Long: stop_loss must be below entry."
            return False
    elif p.signal == "short":
        if p.target >= p.entry:
            r.rejection_reason = "Short: target must be below entry."
            return False
        if p.stop_loss <= p.entry:
            r.rejection_reason = "Short: stop_loss must be above entry."
            return False

    # 3. ATR stop check — adjust if too tight
    effective_stop = p.stop_loss
    if p.atr and p.atr > 0:
        stop_distance = abs(p.entry - p.stop_loss)
        min_distance  = ATR_STOP_MIN_MULT * p.atr
        if stop_distance < min_distance:
            effective_stop = _adjusted_stop(p.entry, p.atr, p.signal)
            r.warnings.append(
                f"Stop too tight ({stop_distance:.4f} < {ATR_STOP_MIN_MULT}×ATR "
                f"= {min_distance:.4f}). Adjusted to {effective_stop:.4f} "
                f"({ATR_STOP_ADJUST_MULT}×ATR from entry)."
            )
            r.adjustments["stop_loss"] = effective_stop
            r.adjusted = True

    # 4. Risk-reward
    rr = _calc_rr(p.entry, p.target, effective_stop)
    if rr < MIN_RR_HARD:
        r.rejection_reason = (
            f"Risk-reward {rr:.2f} < minimum {MIN_RR_HARD}. Trade not worth taking."
        )
        return False
    if rr < MIN_RR_WARN:
        r.warnings.append(
            f"Risk-reward {rr:.2f} is below ideal {MIN_RR_WARN}. "
            "Consider waiting for a better setup."
        )

    r.final_entry  = p.entry
    r.final_target = p.target
    r.final_stop   = effective_stop
    r.risk_reward  = rr
    return True


# ─── Range-trading validator ──────────────────────────────────────────────────

def _validate_range(p: Proposal, r: ValidationResult) -> bool:
    # 1. Trend filter — ADX
    if p.adx is not None:
        if p.adx >= ADX_RANGE_HARD_MAX:
            r.rejection_reason = (
                f"ADX {p.adx:.1f} ≥ {ADX_RANGE_HARD_MAX}: market is trending. "
                "Range trading is NOT appropriate here. Consider trend-following instead."
            )
            return False
        if p.adx >= ADX_RANGE_WARN:
            r.warnings.append(
                f"ADX {p.adx:.1f} is approaching trend territory ({ADX_RANGE_HARD_MAX}). "
                "Breakout risk increasing — watch closely."
            )

    # 2. Boundary confirmation — touch counts
    if p.support_touches is not None and p.support_touches < MIN_SUPPORT_TOUCHES:
        r.rejection_reason = (
            f"Support tested only {p.support_touches} time(s) "
            f"(need ≥ {MIN_SUPPORT_TOUCHES}). Level not confirmed."
        )
        return False
    if p.resistance_touches is not None and p.resistance_touches < MIN_RESISTANCE_TOUCHES:
        r.rejection_reason = (
            f"Resistance tested only {p.resistance_touches} time(s) "
            f"(need ≥ {MIN_RESISTANCE_TOUCHES}). Level not confirmed."
        )
        return False

    # 3. Range width
    if p.support and p.resistance:
        width_pct = (p.resistance - p.support) / p.support * 100
        if width_pct < RANGE_MIN_WIDTH_PCT:
            r.rejection_reason = (
                f"Range width is {width_pct:.2f}% — too narrow (< {RANGE_MIN_WIDTH_PCT}%). "
                "Not enough room to cover costs and a proper stop."
            )
            return False

    # 4. Oscillator confirmation at boundary
    if p.rsi is not None:
        if p.signal == "long":
            if p.rsi > RSI_LONG_REJECT:
                r.rejection_reason = (
                    f"Long at support but RSI {p.rsi:.1f} > {RSI_LONG_REJECT}: "
                    "not oversold. Boundary may not hold."
                )
                return False
            if p.rsi > RSI_LONG_WARN:
                r.warnings.append(
                    f"RSI {p.rsi:.1f} is not deeply oversold. "
                    "Consider waiting for RSI ≤ 30 for stronger confirmation."
                )
        elif p.signal == "short":
            if p.rsi < RSI_SHORT_REJECT:
                r.rejection_reason = (
                    f"Short at resistance but RSI {p.rsi:.1f} < {RSI_SHORT_REJECT}: "
                    "not overbought. Boundary may not hold."
                )
                return False
            if p.rsi < RSI_SHORT_WARN:
                r.warnings.append(
                    f"RSI {p.rsi:.1f} is not deeply overbought. "
                    "Consider waiting for RSI ≥ 70 for stronger confirmation."
                )

    # 5. Stop must be OUTSIDE the range
    effective_stop = r.adjustments.get("stop_loss", p.stop_loss)
    if p.support and p.resistance:
        if p.signal == "long" and effective_stop >= p.support:
            r.rejection_reason = (
                f"Stop {effective_stop:.4f} is inside the range (support={p.support:.4f}). "
                "Stop must be placed below support to survive normal oscillation."
            )
            return False
        if p.signal == "short" and effective_stop <= p.resistance:
            r.rejection_reason = (
                f"Stop {effective_stop:.4f} is inside the range (resistance={p.resistance:.4f}). "
                "Stop must be placed above resistance."
            )
            return False

    # 6. Elevated breakout risk
    if p.breakout_risk == "elevated":
        r.warnings.append(
            "Breakout risk is ELEVATED. Consider standing aside or tightening stops."
        )

    return True


# ─── Trend-following validator ────────────────────────────────────────────────

def _validate_trend(p: Proposal, r: ValidationResult) -> bool:
    # 1. EMA alignment
    if p.ema50 is not None and p.ema200 is not None:
        spread_pct = abs(p.ema50 - p.ema200) / p.ema200

        if spread_pct < EMA_TANGLE_PCT:
            r.warnings.append(
                f"EMA50 ({p.ema50:.2f}) and EMA200 ({p.ema200:.2f}) are virtually "
                f"identical ({spread_pct*100:.2f}% apart). No clear trend — consider waiting."
            )

        if p.signal == "long":
            if p.ema50 < p.ema200:
                r.rejection_reason = (
                    f"Long signal but EMA50 ({p.ema50:.2f}) < EMA200 ({p.ema200:.2f}): "
                    "Death Cross — no uptrend confirmed."
                )
                return False
            if p.price and p.price < p.ema50:
                r.warnings.append(
                    f"Price ({p.price:.2f}) is below EMA50 ({p.ema50:.2f}). "
                    "Entering during a pullback — wait for price to reclaim EMA50 "
                    "or use a pullback entry explicitly."
                )

        elif p.signal == "short":
            if p.ema50 > p.ema200:
                r.rejection_reason = (
                    f"Short signal but EMA50 ({p.ema50:.2f}) > EMA200 ({p.ema200:.2f}): "
                    "Golden Cross — no downtrend confirmed."
                )
                return False
            if p.price and p.price > p.ema50:
                r.warnings.append(
                    f"Price ({p.price:.2f}) is above EMA50 ({p.ema50:.2f}). "
                    "Entering during a bounce — wait for price to fail at EMA50."
                )

    # 2. MACD must align with trend direction (the trend filter overrides MACD)
    if p.macd_histogram is not None:
        if p.signal == "long" and p.macd_histogram < 0:
            r.warnings.append(
                f"MACD histogram {p.macd_histogram:.4f} is negative on a long signal. "
                "Momentum is not confirming the trend — treat as lower-quality entry."
            )
        elif p.signal == "short" and p.macd_histogram > 0:
            r.warnings.append(
                f"MACD histogram {p.macd_histogram:.4f} is positive on a short signal. "
                "Momentum is not confirming the trend — treat as lower-quality entry."
            )

    # 3. Stop sanity check — should not be unreasonably wide
    if p.atr and p.atr > 0:
        stop_distance = abs(p.entry - r.final_stop)
        if stop_distance > 4 * p.atr:
            r.warnings.append(
                f"Stop distance {stop_distance:.4f} > 4×ATR ({4*p.atr:.4f}). "
                "Very wide stop — verify it is anchored to a real swing low/high."
            )

    return True


# ─── Momentum-trading validator ───────────────────────────────────────────────

def _validate_momentum(p: Proposal, r: ValidationResult) -> bool:
    # 1. Price vs 50 EMA (direction bias)
    if p.ema50 is not None and p.price is not None:
        if p.signal == "long" and p.price < p.ema50:
            r.rejection_reason = (
                f"Long momentum: price {p.price:.2f} is below EMA50 ({p.ema50:.2f}). "
                "No upward momentum bias — not a valid momentum long."
            )
            return False
        if p.signal == "short" and p.price > p.ema50:
            r.rejection_reason = (
                f"Short momentum: price {p.price:.2f} is above EMA50 ({p.ema50:.2f}). "
                "No downward momentum bias — not a valid momentum short."
            )
            return False

    # 2. RSI — speed of the move
    if p.rsi is not None:
        if p.signal == "long":
            if p.rsi < RSI_MOM_LONG_MIN:
                r.rejection_reason = (
                    f"Long momentum: RSI {p.rsi:.1f} < {RSI_MOM_LONG_MIN}. "
                    "Insufficient upward momentum."
                )
                return False
            if p.rsi > RSI_MOM_LONG_OVER:
                r.warnings.append(
                    f"RSI {p.rsi:.1f} > {RSI_MOM_LONG_OVER}: move may be overextended. "
                    "Risk of pullback — reduce position size."
                )
        elif p.signal == "short":
            if p.rsi > RSI_MOM_SHORT_MAX:
                r.rejection_reason = (
                    f"Short momentum: RSI {p.rsi:.1f} > {RSI_MOM_SHORT_MAX}. "
                    "Insufficient downward momentum."
                )
                return False
            if p.rsi < RSI_MOM_SHORT_OVER:
                r.warnings.append(
                    f"RSI {p.rsi:.1f} < {RSI_MOM_SHORT_OVER}: short is overextended. "
                    "Risk of bounce — reduce position size."
                )

    # 3. MACD confirmation
    if p.macd_histogram is not None:
        if p.signal == "long" and p.macd_histogram < 0:
            r.warnings.append(
                f"MACD histogram {p.macd_histogram:.4f} is negative on long momentum. "
                "Consider waiting for MACD to cross above zero."
            )
        elif p.signal == "short" and p.macd_histogram > 0:
            r.warnings.append(
                f"MACD histogram {p.macd_histogram:.4f} is positive on short momentum. "
                "Consider waiting for MACD to cross below zero."
            )

    # 4. Volume — non-negotiable for breakouts
    if p.volume_ratio is not None:
        is_breakout = bool(p.new_20d_high or p.new_20d_low)
        if is_breakout and p.volume_ratio < VOLUME_BREAKOUT_MIN:
            r.rejection_reason = (
                f"Breakout without volume: vol_ratio {p.volume_ratio:.2f} "
                f"< {VOLUME_BREAKOUT_MIN}. A valid breakout needs ≥ 120% of average volume."
            )
            return False
        if not is_breakout and p.volume_ratio < 0.8:
            r.warnings.append(
                f"Volume below average (ratio {p.volume_ratio:.2f}). "
                "Low conviction — wait for volume to increase."
            )

    # 5. Trigger candle confirmation
    if p.signal == "long" and p.new_20d_high is False:
        r.warnings.append(
            "Price has not printed a new 20-day high. "
            "Wait for the trigger candle to close before entering."
        )
    if p.signal == "short" and p.new_20d_low is False:
        r.warnings.append(
            "Price has not printed a new 20-day low. "
            "Wait for the trigger candle to close before entering."
        )

    return True


# ─── Portfolio limits ─────────────────────────────────────────────────────────

MAX_OPEN_POSITIONS = 3
# Portfolio caps are MARGIN-based, not notional-based. With leverage (up to 20x)
# a correctly risk-sized intraday trade has a notional several times the equity
# (tight stop + 3–5% risk → notional ≈ risk / stop_pct), so a raw-notional cap
# would reject essentially every intraday trade. Margin = notional / leverage is
# the real capital at stake, so we cap that instead.
MAX_TOTAL_MARGIN_PCT     = 0.60   # total margin used ≤ 60% of equity (40% buffer)
MAX_PER_ASSET_MARGIN_PCT = 0.40   # margin on a single asset ≤ 40% of equity


# ─── RiskManager ──────────────────────────────────────────────────────────────

_STRATEGY_VALIDATORS = {
    "range-trading":    _validate_range,
    "trend-following":  _validate_trend,
    "momentum-trading": _validate_momentum,
}


class RiskManager:
    _PORTFOLIO_PATH = Path(__file__).parent / "data" / "portfolio_state.json"
    _LOG_PATH       = Path(__file__).parent / "logs" / "proposals.jsonl"

    def __init__(self):
        self._portfolio = self._load_portfolio()

    def _load_portfolio(self) -> dict:
        if self._PORTFOLIO_PATH.exists():
            try:
                return json.loads(self._PORTFOLIO_PATH.read_text())
            except Exception:
                pass
        return {"positions": [], "total_equity": None}

    def _log(self, result: ValidationResult) -> None:
        self._LOG_PATH.parent.mkdir(exist_ok=True)
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **result.to_dict()}
        with self._LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    @staticmethod
    def _pos_asset(pos: dict):
        return pos.get("asset") or pos.get("symbol")

    @staticmethod
    def _pos_side(pos: dict):
        # Snapshot schema (portfolio.from_coinvest) uses "side"; older/other
        # payloads may use "signal". Tolerate both so a real snapshot never crashes.
        return pos.get("side") or pos.get("signal")

    @staticmethod
    def _pos_notional(pos: dict) -> float:
        # Snapshot uses "size_usd"; risk_manager's own logs use "notional".
        val = pos.get("size_usd")
        if val is None:
            val = pos.get("notional")
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _pos_margin(pos: dict) -> float:
        """Capital actually committed = notional / leverage. Unknown leverage is
        treated as 1x (i.e. margin == notional), the conservative assumption."""
        notional = RiskManager._pos_notional(pos)
        lev = pos.get("leverage", pos.get("lev"))
        try:
            lev = float(lev)
        except (TypeError, ValueError):
            lev = None
        return notional / lev if lev and lev > 0 else notional

    def _validate_portfolio(self, p: Proposal, r: ValidationResult) -> bool:
        positions = self._portfolio.get("positions", [])

        if len(positions) >= MAX_OPEN_POSITIONS:
            r.rejection_reason = (
                f"Portfolio limit: {len(positions)} open position(s) already "
                f"(max {MAX_OPEN_POSITIONS}). Close one before opening another."
            )
            return False

        for pos in positions:
            pos_side = self._pos_side(pos)
            if self._pos_asset(pos) == p.asset and pos_side != p.signal:
                r.rejection_reason = (
                    f"Conflicting positions: cannot open {p.signal} on {p.asset} "
                    f"while a {pos_side} position is already open."
                )
                return False

        equity = self._portfolio.get("total_equity")
        if equity:
            asset_margin = sum(
                self._pos_margin(pos) for pos in positions
                if self._pos_asset(pos) == p.asset
            )
            total_margin = sum(self._pos_margin(pos) for pos in positions)
            if asset_margin / equity >= MAX_PER_ASSET_MARGIN_PCT:
                r.rejection_reason = (
                    f"Per-asset margin limit: {p.asset} already using "
                    f"{asset_margin/equity*100:.1f}% of equity as margin "
                    f"(max {MAX_PER_ASSET_MARGIN_PCT*100:.0f}%)."
                )
                return False
            if total_margin / equity >= MAX_TOTAL_MARGIN_PCT:
                r.rejection_reason = (
                    f"Total margin limit: portfolio already using "
                    f"{total_margin/equity*100:.1f}% of equity as margin "
                    f"(max {MAX_TOTAL_MARGIN_PCT*100:.0f}%)."
                )
                return False

        return True

    def validate(self, proposal: dict | Proposal) -> ValidationResult:
        if isinstance(proposal, dict):
            raw = proposal.copy()
            p   = Proposal.from_dict(proposal)
        else:
            p   = proposal
            raw = {f: getattr(p, f) for f in p.__dataclass_fields__}

        r = ValidationResult(approved=False, confidence=p.confidence, original=raw)

        if p.signal == "no_trade":
            r.approved = True
            r.warnings.append("Signal is no_trade — no position to validate.")
            self._log(r)
            return r

        if not _validate_universal(p, r):
            self._log(r)
            return r

        if not self._validate_portfolio(p, r):
            self._log(r)
            return r

        validator = _STRATEGY_VALIDATORS.get(p.strategy)
        if validator is None:
            r.rejection_reason = f"Unknown strategy '{p.strategy}'."
            self._log(r)
            return r

        if not validator(p, r):
            self._log(r)
            return r

        r.approved  = True
        r.validated = {
            "strategy":    p.strategy,
            "asset":       p.asset,
            "signal":      p.signal,
            "timeframe":   p.timeframe,
            "entry":       r.final_entry,
            "target":      r.final_target,
            "stop_loss":   r.final_stop,
            "risk_reward": r.risk_reward,
        }
        self._log(r)
        return r


def validate(proposal: dict | Proposal) -> ValidationResult:
    """Module-level convenience wrapper around RiskManager.validate()."""
    return RiskManager().validate(proposal)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python risk_manager.py <proposal.json>")
        sys.exit(1)
    proposal = json.loads(Path(sys.argv[1]).read_text())
    result   = RiskManager().validate(proposal)
    print(json.dumps(result.to_dict(), indent=2))
    sys.exit(0 if result.approved else 1)


if __name__ == "__main__":
    main()
