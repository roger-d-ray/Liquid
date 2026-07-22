"""Resolve the instrument that Co-Invest will actually trade.

The repository has no exchange credentials and cannot call the Co-Invest MCP
from Python.  The MCP agent therefore passes its runtime catalogue (or a
provider object exposing the same information) to :class:`ExecutionInstrumentResolver`.
No venue or symbol is inferred from the signal asset.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


RESOLVED = "resolved"
BLOCKED = "blocked"
UNSUPPORTED = "unsupported"
MARKET_TYPES = {"spot", "perpetual", "future"}
DEFAULT_AUDIT_PATH = (
    Path(__file__).resolve().parent / "logs" / "execution_resolution.jsonl"
)


class ExecutionResolutionError(ValueError):
    """Raised when code tries to use a proposal that is not executable."""


@dataclass(frozen=True)
class ResolverConfig:
    max_data_age_seconds: float = 30.0
    max_dislocation_bps: float = 25.0

    @classmethod
    def from_env(cls) -> "ResolverConfig":
        return cls(
            max_data_age_seconds=float(
                os.getenv("EXECUTION_DATA_MAX_AGE_SECONDS", "30")
            ),
            max_dislocation_bps=float(
                os.getenv("MAX_EXECUTION_DISLOCATION_BPS", "25")
            ),
        )


@dataclass(frozen=True)
class ExecutionInstrument:
    execution_venue: str
    instrument_id: str
    base_asset: str
    quote_asset: str
    market_type: str
    collateral_currency: Optional[str]
    contract_multiplier: Optional[float]
    tick_size: float
    lot_size: float
    minimum_notional: float
    mark_price: Optional[float]
    index_price: Optional[float]
    last_price: float
    stop_trigger_type: Optional[str]
    margin_mode: Optional[str]
    fee_tier: Any = None


@dataclass
class MarketContext:
    signal_venue: Optional[str]
    derivatives_venue: Optional[str]
    execution_venue: Optional[str]
    resolution_status: str
    signal_price: Optional[float]
    signal_price_type: Optional[str]
    execution_price: Optional[float] = None
    execution_price_type: Optional[str] = None
    execution_price_observed_at: Optional[str] = None
    execution_data_age_seconds: Optional[float] = None
    dislocation_bps: Optional[float] = None
    absolute_dislocation_bps: Optional[float] = None
    max_data_age_seconds: Optional[float] = None
    max_dislocation_bps: Optional[float] = None
    instrument: Optional[ExecutionInstrument] = None
    catalog_source: Optional[str] = None
    reasons: list[str] = field(default_factory=list)
    live_trading_allowed: bool = False
    resolved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)


_FIELD_ALIASES = {
    "execution_venue": ("execution_venue", "executionVenue", "venue", "exchange"),
    "instrument_id": ("instrument_id", "instrumentId", "symbol", "id"),
    "base_asset": ("base_asset", "baseAsset", "base", "underlying"),
    "quote_asset": ("quote_asset", "quoteAsset", "quote"),
    "market_type": ("market_type", "marketType", "type"),
    "collateral_currency": (
        "collateral_currency", "collateralCurrency", "collateral", "settleAsset",
    ),
    "contract_multiplier": (
        "contract_multiplier", "contractMultiplier", "multiplier", "contractSize",
    ),
    "tick_size": ("tick_size", "tickSize", "priceIncrement"),
    "lot_size": ("lot_size", "lotSize", "quantityIncrement", "stepSize"),
    "minimum_notional": (
        "minimum_notional", "minimumNotional", "minNotional", "minOrderValue",
    ),
    "mark_price": ("mark_price", "markPrice"),
    "index_price": ("index_price", "indexPrice"),
    "last_price": ("last_price", "lastPrice", "price"),
    "bid_price": ("bid_price", "bidPrice", "bestBid"),
    "ask_price": ("ask_price", "askPrice", "bestAsk"),
    "stop_trigger_type": (
        "stop_trigger_type", "stopTriggerType", "triggerPriceType",
    ),
    "margin_mode": ("margin_mode", "marginMode"),
    "fee_tier": ("fee_tier", "feeTier", "fees"),
    "observed_at": (
        "observed_at", "observedAt", "updated_at", "updatedAt", "timestamp",
        "price_timestamp", "priceTimestamp",
    ),
    "catalog_source": ("catalog_source", "catalogSource", "source"),
}

_NESTED_CONTAINERS = (
    "instrument", "contract", "contract_specs", "contractSpecs", "specs",
    "market", "ticker", "prices", "metadata",
)


def _lookup(data: Mapping[str, Any], field: str):
    aliases = _FIELD_ALIASES[field]
    for key in aliases:
        if key in data and data[key] is not None:
            return data[key]
    for container in _NESTED_CONTAINERS:
        nested = data.get(container)
        if isinstance(nested, Mapping):
            for key in aliases:
                if key in nested and nested[key] is not None:
                    return nested[key]
    return None


def _text(value) -> Optional[str]:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _market_type(value) -> Optional[str]:
    value = (_text(value) or "").lower()
    aliases = {
        "perp": "perpetual", "swap": "perpetual", "perpetual_swap": "perpetual",
        "futures": "future", "cash": "spot",
    }
    value = aliases.get(value, value)
    return value if value in MARKET_TYPES else None


def _utc_datetime(value) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


class RuntimeCatalog:
    """Adapter for a JSON catalogue captured from Co-Invest at runtime."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = payload

    def get_execution_venue(self):
        return _lookup(self.payload, "execution_venue")

    def list_instruments(self, venue: str) -> list[Mapping[str, Any]]:
        instruments = self.payload.get("instruments") or self.payload.get("markets")
        if isinstance(instruments, list):
            return instruments
        if isinstance(instruments, Mapping):
            result = []
            for instrument_id, value in instruments.items():
                if isinstance(value, Mapping):
                    item = dict(value)
                    item.setdefault("instrument_id", instrument_id)
                    result.append(item)
            return result
        for venue_data in self.payload.get("venues", ()) or ():
            if not isinstance(venue_data, Mapping):
                continue
            venue_id = _lookup(venue_data, "execution_venue")
            if _same(venue_id, venue):
                nested = venue_data.get("instruments") or venue_data.get("markets") or ()
                if isinstance(nested, Mapping):
                    return [
                        {"instrument_id": key, **dict(value)}
                        for key, value in nested.items() if isinstance(value, Mapping)
                    ]
                return list(nested)
        return []

    def get_market_snapshot(self, venue: str, instrument_id: str) -> Mapping[str, Any]:
        snapshots = self.payload.get("snapshots") or self.payload.get("tickers") or {}
        if isinstance(snapshots, Mapping):
            direct = snapshots.get(instrument_id)
            if isinstance(direct, Mapping):
                return direct
            direct = snapshots.get(f"{venue}:{instrument_id}")
            if isinstance(direct, Mapping):
                return direct
        return {}

    @property
    def catalog_source(self):
        return _lookup(self.payload, "catalog_source") or "runtime_catalog"


def _same(left, right) -> bool:
    return (
        bool(_text(left) and _text(right))
        and str(left).strip().casefold() == str(right).strip().casefold()
    )


class ExecutionInstrumentResolver:
    """Select and validate an execution instrument without parsing its symbol."""

    def __init__(
        self,
        provider: Any,
        config: Optional[ResolverConfig] = None,
        audit_path: Optional[Path | str] = DEFAULT_AUDIT_PATH,
    ):
        self.provider = RuntimeCatalog(provider) if isinstance(provider, Mapping) else provider
        self.config = config or ResolverConfig.from_env()
        self.audit_path = Path(audit_path) if audit_path else None

    def _provider_value(self, name: str):
        value = getattr(self.provider, name, None)
        return value() if callable(value) else value

    def _instruments(self, venue: str) -> list[Mapping[str, Any]]:
        method = getattr(self.provider, "list_instruments", None)
        if not callable(method):
            raise NotImplementedError("catalogue does not expose list_instruments")
        try:
            result = method(venue)
        except TypeError:
            result = method()
        return [item for item in (result or ()) if isinstance(item, Mapping)]

    def _snapshot(self, venue: str, instrument_id: str) -> Mapping[str, Any]:
        method = getattr(self.provider, "get_market_snapshot", None)
        if not callable(method):
            return {}
        try:
            result = method(venue, instrument_id)
        except TypeError:
            result = method(instrument_id)
        return result if isinstance(result, Mapping) else {}

    def _record(self, context: MarketContext) -> MarketContext:
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(context.to_dict(), ensure_ascii=False) + "\n")
        return context

    def resolve(
        self,
        *,
        base_asset: str,
        signal_price: float,
        signal_venue: Optional[str],
        derivatives_venue: Optional[str],
        side: str,
        quote_asset: Optional[str] = None,
        market_type: Optional[str] = None,
        execution_venue: Optional[str] = None,
        instrument_id: Optional[str] = None,
        signal_price_type: str = "last",
        now: Optional[datetime] = None,
    ) -> MarketContext:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            runtime_venue = self._provider_value("get_execution_venue")
        except Exception as exc:
            runtime_venue = None
            venue_error = str(exc)
        else:
            venue_error = None
        if runtime_venue is None:
            try:
                runtime_venue = self._provider_value("execution_venue")
            except Exception:
                pass
        venue = _text(execution_venue) or _text(runtime_venue)
        catalog_source = getattr(self.provider, "catalog_source", None)
        if callable(catalog_source):
            catalog_source = catalog_source()
        base_context = dict(
            signal_venue=_text(signal_venue),
            derivatives_venue=_text(derivatives_venue),
            execution_venue=venue,
            signal_price=_number(signal_price),
            signal_price_type=_text(signal_price_type),
            max_data_age_seconds=self.config.max_data_age_seconds,
            max_dislocation_bps=self.config.max_dislocation_bps,
            catalog_source=_text(catalog_source),
        )
        if not venue:
            reason = "execution venue is unknown"
            if venue_error:
                reason += f": {venue_error}"
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED, reasons=[reason]
            ))
        if runtime_venue and execution_venue and not _same(runtime_venue, execution_venue):
            return self._record(MarketContext(
                **base_context,
                resolution_status=BLOCKED,
                reasons=[
                    f"requested execution venue '{execution_venue}' does not match "
                    f"runtime venue '{runtime_venue}'"
                ],
            ))
        if not base_context["signal_venue"]:
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED,
                reasons=["signal venue is unknown"],
            ))

        try:
            instruments = self._instruments(venue)
        except (NotImplementedError, AttributeError) as exc:
            return self._record(MarketContext(
                **base_context, resolution_status=UNSUPPORTED,
                reasons=[f"Co-Invest catalogue unsupported: {exc}"],
            ))
        except Exception as exc:
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED,
                reasons=[f"could not query execution catalogue: {exc}"],
            ))

        requested_market_type = _market_type(market_type) if market_type else None
        if market_type and requested_market_type is None:
            return self._record(MarketContext(
                **base_context,
                resolution_status=UNSUPPORTED,
                reasons=[f"unsupported requested market_type '{market_type}'"],
            ))
        candidates = []
        for item in instruments:
            item_venue = _lookup(item, "execution_venue")
            if item_venue and not _same(item_venue, venue):
                continue
            item_id = _text(_lookup(item, "instrument_id"))
            if instrument_id:
                if _same(item_id, instrument_id):
                    candidates.append(item)
                continue
            bases = [_lookup(item, "base_asset")]
            aliases = item.get("base_asset_aliases") or item.get("baseAssetAliases") or ()
            if isinstance(aliases, str):
                aliases = [aliases]
            bases.extend(aliases)
            if not any(_same(candidate_base, base_asset) for candidate_base in bases):
                continue
            if quote_asset and not _same(_lookup(item, "quote_asset"), quote_asset):
                continue
            if requested_market_type and _market_type(_lookup(item, "market_type")) != requested_market_type:
                continue
            candidates.append(item)

        if len(candidates) != 1:
            detail = "not found" if not candidates else f"ambiguous ({len(candidates)} matches)"
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED,
                reasons=[f"execution instrument is not identified: {detail}"],
            ))

        selected = candidates[0]
        selected_id = _text(_lookup(selected, "instrument_id"))
        try:
            snapshot = self._snapshot(venue, selected_id or "")
        except Exception as exc:
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED,
                reasons=[f"could not query execution prices: {exc}"],
            ))
        combined = dict(selected)
        combined.update(snapshot)

        mt = _market_type(_lookup(combined, "market_type"))
        values = {
            "instrument_id": selected_id,
            "base_asset": _text(_lookup(combined, "base_asset")),
            "quote_asset": _text(_lookup(combined, "quote_asset")),
            "market_type": mt,
            "collateral_currency": _text(_lookup(combined, "collateral_currency")),
            "contract_multiplier": _number(_lookup(combined, "contract_multiplier")),
            "tick_size": _number(_lookup(combined, "tick_size")),
            "lot_size": _number(_lookup(combined, "lot_size")),
            "minimum_notional": _number(_lookup(combined, "minimum_notional")),
            "mark_price": _number(_lookup(combined, "mark_price")),
            "index_price": _number(_lookup(combined, "index_price")),
            "last_price": _number(_lookup(combined, "last_price")),
            "stop_trigger_type": _text(_lookup(combined, "stop_trigger_type")),
            "margin_mode": _text(_lookup(combined, "margin_mode")),
            "fee_tier": _lookup(combined, "fee_tier"),
        }
        selected_bases = [values["base_asset"]]
        selected_aliases = (
            selected.get("base_asset_aliases")
            or selected.get("baseAssetAliases")
            or ()
        )
        if isinstance(selected_aliases, str):
            selected_aliases = [selected_aliases]
        selected_bases.extend(selected_aliases)
        intent_mismatches = []
        if not any(_same(value, base_asset) for value in selected_bases):
            intent_mismatches.append("base_asset")
        if quote_asset and not _same(values["quote_asset"], quote_asset):
            intent_mismatches.append("quote_asset")
        if requested_market_type and values["market_type"] != requested_market_type:
            intent_mismatches.append("market_type")
        if intent_mismatches:
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED,
                reasons=["resolved instrument conflicts with signal intent: " + ", ".join(intent_mismatches)],
            ))
        common_required = (
            "instrument_id", "base_asset", "quote_asset", "market_type",
            "tick_size", "lot_size", "minimum_notional", "last_price",
        )
        derivative_required = (
            "collateral_currency", "contract_multiplier", "mark_price",
            "index_price", "stop_trigger_type", "margin_mode",
        )
        missing = [name for name in common_required if values.get(name) is None]
        if mt in {"perpetual", "future"}:
            missing += [name for name in derivative_required if values.get(name) is None]
        if missing:
            return self._record(MarketContext(
                **base_context, resolution_status=UNSUPPORTED,
                reasons=[
                    "Co-Invest does not expose indispensable instrument data: "
                    + ", ".join(missing)
                ],
            ))

        ask = _number(_lookup(combined, "ask_price"))
        bid = _number(_lookup(combined, "bid_price"))
        if str(side).strip().lower() == "long" and ask is not None:
            execution_price, price_type = ask, "ask"
        elif str(side).strip().lower() == "short" and bid is not None:
            execution_price, price_type = bid, "bid"
        else:
            execution_price, price_type = values["last_price"], "last"

        observed_at = _utc_datetime(_lookup(combined, "observed_at"))
        if observed_at is None:
            return self._record(MarketContext(
                **base_context, resolution_status=UNSUPPORTED,
                reasons=["Co-Invest does not expose an execution-price timestamp"],
            ))
        age = (now - observed_at).total_seconds()
        if age < -1:
            return self._record(MarketContext(
                **base_context, resolution_status=BLOCKED,
                reasons=["execution-price timestamp is in the future"],
            ))
        signal_value = _number(signal_price)
        if signal_value is None:
            return self._record(MarketContext(
                **base_context, resolution_status=UNSUPPORTED,
                reasons=["signal price is missing or invalid"],
            ))
        displacement = (execution_price / signal_value - 1.0) * 10_000
        instrument = ExecutionInstrument(execution_venue=venue, **values)
        result_fields = dict(
            **base_context,
            execution_price=execution_price,
            execution_price_type=price_type,
            execution_price_observed_at=observed_at.isoformat(),
            execution_data_age_seconds=round(max(age, 0.0), 3),
            dislocation_bps=round(displacement, 4),
            absolute_dislocation_bps=round(abs(displacement), 4),
            instrument=instrument,
        )
        if age > self.config.max_data_age_seconds:
            return self._record(MarketContext(
                **result_fields, resolution_status=BLOCKED,
                reasons=[
                    f"execution data is stale ({age:.1f}s > "
                    f"{self.config.max_data_age_seconds:.1f}s)"
                ],
            ))
        if abs(displacement) > self.config.max_dislocation_bps:
            return self._record(MarketContext(
                **result_fields, resolution_status=BLOCKED,
                reasons=[
                    f"execution-price dislocation is {abs(displacement):.2f} bps "
                    f"(max {self.config.max_dislocation_bps:.2f} bps)"
                ],
            ))
        return self._record(MarketContext(
            **result_fields, resolution_status=RESOLVED,
            live_trading_allowed=True,
        ))

    def resolve_proposal(
        self,
        proposal: Mapping[str, Any],
        *,
        signal_venue: Optional[str],
        derivatives_venue: Optional[str],
        execution_venue: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        """Attach the context and use its executable price as proposed entry."""
        out = deepcopy(dict(proposal))
        signal_price = out.get("signal_price", out.get("price", out.get("entry")))
        context = self.resolve(
            base_asset=out.get("base_asset", out.get("asset")),
            quote_asset=out.get("quote_asset"),
            market_type=out.get("market_type"),
            instrument_id=out.get("instrument_id"),
            signal_price=signal_price,
            signal_price_type=out.get("signal_price_type", "last"),
            signal_venue=signal_venue,
            derivatives_venue=derivatives_venue,
            execution_venue=execution_venue,
            side=out.get("signal", out.get("side")),
            now=now,
        )
        out["signal_price"] = signal_price
        out["market_context"] = context.to_dict()
        if context.instrument:
            out.update({
                "execution_venue": context.execution_venue,
                "instrument_id": context.instrument.instrument_id,
                "base_asset": context.instrument.base_asset,
                "quote_asset": context.instrument.quote_asset,
                "market_type": context.instrument.market_type,
            })
        if context.resolution_status == RESOLVED:
            out["entry"] = context.execution_price
            out["execution_price"] = context.execution_price
            out["execution_price_type"] = context.execution_price_type
        return out


def execution_block_reason(proposal: Mapping[str, Any]) -> Optional[str]:
    context = proposal.get("market_context")
    if not isinstance(context, Mapping):
        return "execution MarketContext is missing"
    if context.get("resolution_status") != RESOLVED or context.get("live_trading_allowed") is not True:
        reasons = context.get("reasons") or ["execution context is not resolved"]
        return "; ".join(str(reason) for reason in reasons)
    instrument = context.get("instrument")
    if not isinstance(instrument, Mapping):
        return "resolved MarketContext has no instrument"
    if not context.get("execution_venue"):
        return "resolved MarketContext is missing execution_venue"
    if not context.get("signal_venue"):
        return "resolved MarketContext is missing signal_venue"
    if not _same(instrument.get("execution_venue"), context.get("execution_venue")):
        return "instrument execution_venue conflicts with MarketContext"
    if _market_type(instrument.get("market_type")) != instrument.get("market_type"):
        return "resolved MarketContext has invalid market_type"
    required = (
        "instrument_id", "base_asset", "quote_asset", "market_type", "tick_size",
        "lot_size", "minimum_notional", "last_price",
    )
    missing = [name for name in required if instrument.get(name) is None]
    if instrument.get("market_type") in {"perpetual", "future"}:
        derivative_required = (
            "collateral_currency", "contract_multiplier", "mark_price",
            "index_price", "stop_trigger_type", "margin_mode",
        )
        missing += [name for name in derivative_required if instrument.get(name) is None]
    if missing:
        return "resolved MarketContext is missing " + ", ".join(missing)
    for numeric_field in ("tick_size", "lot_size", "minimum_notional", "last_price"):
        if _number(instrument.get(numeric_field)) is None:
            return f"resolved MarketContext has invalid {numeric_field}"
    if instrument.get("market_type") in {"perpetual", "future"}:
        for numeric_field in ("contract_multiplier", "mark_price", "index_price"):
            if _number(instrument.get(numeric_field)) is None:
                return f"resolved MarketContext has invalid {numeric_field}"
    if _number(context.get("signal_price")) is None:
        return "resolved MarketContext has no signal price"
    if _number(context.get("execution_price")) is None:
        return "resolved MarketContext has no execution price"
    if context.get("execution_price_type") not in {"ask", "bid", "last"}:
        return "resolved MarketContext has no typed execution price"
    if not context.get("execution_price_observed_at"):
        return "resolved MarketContext has no execution-price timestamp"
    try:
        leverage = float(proposal.get("leverage") or 1)
    except (TypeError, ValueError):
        return "proposal leverage is invalid"
    if instrument.get("market_type") == "spot" and leverage != 1:
        return "spot execution does not support derivative leverage"
    age = context.get("execution_data_age_seconds")
    max_age = context.get("max_data_age_seconds")
    if age is None or max_age is None:
        return "resolved MarketContext has no execution-data freshness limit"
    try:
        if float(age) > float(max_age):
            return "execution data is stale"
    except (TypeError, ValueError):
        return "resolved MarketContext has invalid execution-data freshness"
    displacement = context.get("absolute_dislocation_bps")
    max_displacement = context.get("max_dislocation_bps")
    if context.get("dislocation_bps") is None:
        return "resolved MarketContext has no signed dislocation"
    if displacement is None or max_displacement is None:
        return "resolved MarketContext has no dislocation limit"
    try:
        if float(displacement) > float(max_displacement):
            return "execution-price dislocation exceeds its configured limit"
    except (TypeError, ValueError):
        return "resolved MarketContext has invalid dislocation data"
    return None


def assert_executable_proposal(proposal: Mapping[str, Any]) -> None:
    reason = execution_block_reason(proposal)
    if reason:
        raise ExecutionResolutionError(reason)


def build_execution_order(proposal: Mapping[str, Any]) -> dict:
    """Build the Co-Invest order arguments from the resolved instrument only."""
    assert_executable_proposal(proposal)
    context = proposal["market_context"]
    instrument = context["instrument"]
    size = proposal.get("size_usd", proposal.get("size"))
    if _number(size) is None:
        raise ExecutionResolutionError("executable proposal has no positive size_usd")
    if float(size) < float(instrument["minimum_notional"]):
        raise ExecutionResolutionError(
            f"size_usd {float(size):g} is below minimum_notional "
            f"{float(instrument['minimum_notional']):g}"
        )
    signal = str(proposal.get("signal", proposal.get("side", ""))).lower()
    if signal not in {"long", "short"}:
        raise ExecutionResolutionError(f"unsupported execution side '{signal}'")
    return {
        "symbol": instrument["instrument_id"],
        "side": "buy" if signal == "long" else "sell",
        "size": float(size),
        "leverage": float(proposal.get("leverage") or 1),
        "type": "market",
        "tp": proposal.get("target", proposal.get("tp")),
        "sl": proposal.get("stop_loss", proposal.get("sl")),
        "reasoning": proposal.get("motivation", proposal.get("reason", "")),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Resolve a Co-Invest execution instrument")
    parser.add_argument("proposal")
    parser.add_argument("catalog")
    parser.add_argument("--signal-venue", required=True)
    parser.add_argument("--derivatives-venue")
    parser.add_argument("--execution-venue")
    parser.add_argument("--audit-log", default="logs/execution_resolution.jsonl")
    args = parser.parse_args()
    proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    resolver = ExecutionInstrumentResolver(catalog, audit_path=args.audit_log)
    result = resolver.resolve_proposal(
        proposal,
        signal_venue=args.signal_venue,
        derivatives_venue=args.derivatives_venue,
        execution_venue=args.execution_venue,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["market_context"]["resolution_status"] == RESOLVED else 2


if __name__ == "__main__":
    raise SystemExit(main())
