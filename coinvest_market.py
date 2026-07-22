"""Strict adapter for the read-only Co-Invest market tools.

The MCP agent calls ``search_markets``, ``analyze_market`` and
``show_orderbook``.  This module consumes those captured responses and exposes
the small provider interface required by :class:`ExecutionInstrumentResolver`.
It deliberately does not parse market symbols or invent exchange metadata that
Co-Invest does not return.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


class CoinvestPayloadError(ValueError):
    """Raised when captured Co-Invest responses cannot prove one market."""


def _tool_result(payload: Mapping[str, Any], tool_name: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CoinvestPayloadError(f"{tool_name} response must be an object")
    result = payload.get("result")
    if isinstance(result, Mapping):
        payload = result
    return payload


def _structured(payload: Mapping[str, Any], tool_name: str) -> Mapping[str, Any]:
    result = _tool_result(payload, tool_name)
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        raise CoinvestPayloadError(
            f"{tool_name} response has no structuredContent object"
        )
    return structured


def _search_text(payload: Mapping[str, Any]) -> str:
    result = _tool_result(payload, "search_markets")
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping) and isinstance(structured.get("text"), str):
        return structured["text"].strip()
    for block in result.get("content") or ():
        if isinstance(block, Mapping) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise CoinvestPayloadError("search_markets response contains no text evidence")


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _positive(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _utc_datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds):
            return None
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            result = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            result = datetime.fromisoformat(
                str(value).strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _book_price(levels: Any, *, side: str) -> Optional[float]:
    if not isinstance(levels, list):
        return None
    prices = [
        price
        for level in levels
        if isinstance(level, Mapping)
        if _positive(level.get("sz")) is not None
        for price in [_positive(level.get("px"))]
        if price is not None
    ]
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)


class CoinvestRuntimeAdapter:
    """Adapt real Co-Invest tool responses without symbol inference.

    ``search_markets`` currently provides human-readable discovery evidence
    only.  Machine identity is therefore accepted only when ``analyze_market``
    echoes the same non-empty symbol in both ``symbol`` and ``ticker.coin``, and
    ``show_orderbook`` independently echoes it in ``book.coin``.
    """

    def __init__(
        self,
        market_search_payload: Mapping[str, Any],
        analyze_market_payload: Mapping[str, Any],
        order_book_payload: Mapping[str, Any],
        *,
        received_at: datetime,
        execution_venue: str = "liquid",
    ):
        venue = _text(execution_venue)
        if not venue:
            raise CoinvestPayloadError("execution venue is missing")
        received = _utc_datetime(received_at)
        if received is None:
            raise CoinvestPayloadError("received_at must be timezone-aware")

        self.search_evidence = _search_text(market_search_payload)
        analysis = _structured(analyze_market_payload, "analyze_market")
        ticker = analysis.get("ticker")
        if not isinstance(ticker, Mapping):
            raise CoinvestPayloadError("analyze_market ticker is missing")
        analysis_symbol = _text(analysis.get("symbol"))
        ticker_symbol = _text(ticker.get("coin"))
        if not analysis_symbol or not ticker_symbol or analysis_symbol != ticker_symbol:
            raise CoinvestPayloadError(
                "analyze_market symbol and ticker.coin do not identify one market"
            )

        order_book = _structured(order_book_payload, "show_orderbook")
        book = order_book.get("book")
        if not isinstance(book, Mapping):
            raise CoinvestPayloadError("show_orderbook book is missing")
        book_symbol = _text(book.get("coin"))
        if book_symbol != analysis_symbol:
            raise CoinvestPayloadError(
                "show_orderbook book.coin conflicts with analyze_market symbol"
            )

        maximum_leverage = _positive(ticker.get("maxLeverage"))
        if maximum_leverage is None:
            raise CoinvestPayloadError("analyze_market maxLeverage is missing")
        raw_book_time = book.get("time")
        observed = _utc_datetime(raw_book_time)
        if raw_book_time is not None and observed is None:
            raise CoinvestPayloadError("show_orderbook book.time is invalid")
        if observed is None:
            observed = received
        best_bid = _book_price(book.get("bids"), side="bid")
        best_ask = _book_price(book.get("asks"), side="ask")
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            raise CoinvestPayloadError("show_orderbook contains a crossed book")

        self.execution_venue = venue
        self.catalog_source = (
            "coinvest:search_markets+analyze_market+show_orderbook"
        )
        self.instrument_id = analysis_symbol
        self._instrument = {
            "execution_venue": venue,
            "instrument_id": analysis_symbol,
            "base_asset": ticker_symbol,
            "maximum_leverage": maximum_leverage,
            "mark_price": _positive(ticker.get("markPx")),
        }
        self._snapshot = {
            "bestBid": best_bid,
            "bestAsk": best_ask,
            "markPrice": _positive(ticker.get("markPx")),
            "observedAt": observed.isoformat(),
            "timestamp_source": (
                "show_orderbook.book.time"
                if raw_book_time is not None
                else "client_received_at"
            ),
        }

    def get_execution_venue(self) -> str:
        return self.execution_venue

    def list_instruments(self, venue: str) -> list[Mapping[str, Any]]:
        if str(venue).strip().casefold() != self.execution_venue.casefold():
            return []
        return [dict(self._instrument)]

    def get_market_snapshot(
        self, venue: str, instrument_id: str
    ) -> Mapping[str, Any]:
        if str(venue).strip().casefold() != self.execution_venue.casefold():
            raise CoinvestPayloadError("snapshot venue does not match adapter venue")
        if str(instrument_id).strip() != self.instrument_id:
            raise CoinvestPayloadError(
                "snapshot instrument does not match the confirmed Co-Invest symbol"
            )
        return dict(self._snapshot)

    def to_runtime_catalog(self) -> dict:
        """Return the generic resolver payload, useful for audit/CLI handoff."""
        return {
            "execution_venue": self.execution_venue,
            "catalog_source": self.catalog_source,
            "instruments": [dict(self._instrument)],
            "snapshots": {self.instrument_id: dict(self._snapshot)},
        }
