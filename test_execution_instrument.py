import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import risk_manager
import telegram_notify
from execution_instrument import (
    BLOCKED,
    RESOLVED,
    UNSUPPORTED,
    ExecutionResolutionError,
    ExecutionInstrumentResolver,
    ResolverConfig,
    build_execution_order,
)


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


class MockCatalog:
    def __init__(self, venue, instruments, snapshots):
        self.venue = venue
        self.instruments = instruments
        self.snapshots = snapshots
        self.catalog_source = "coinvest_mock"

    def get_execution_venue(self):
        return self.venue

    def list_instruments(self, venue):
        return list(self.instruments)

    def get_market_snapshot(self, venue, instrument_id):
        return self.snapshots[instrument_id]


def instrument(market_type="perpetual", instrument_id="XBT-USD-SWAP", **overrides):
    value = {
        "venue": "vertex",
        "instrumentId": instrument_id,
        "baseAsset": "BTC",
        "quoteAsset": "USD",
        "marketType": market_type,
        "tickSize": "0.1",
        "lotSize": "0.001",
        "minNotional": "10",
    }
    if market_type != "spot":
        value.update({
            "collateralCurrency": "USDC",
            "contractMultiplier": "1",
            "stopTriggerType": "mark",
            "marginMode": "cross",
        })
    value.update(overrides)
    return value


def snapshot(price=100.0, **overrides):
    value = {
        "markPrice": price,
        "indexPrice": price - 0.02,
        "lastPrice": price,
        "bestBid": price - 0.05,
        "bestAsk": price + 0.05,
        "observedAt": NOW.isoformat(),
        "feeTier": {"maker_bps": 1, "taker_bps": 4},
    }
    value.update(overrides)
    return value


class ExecutionInstrumentResolverTests(unittest.TestCase):
    def resolver(self, instruments, snapshots, venue="vertex", **config):
        return ExecutionInstrumentResolver(
            MockCatalog(venue, instruments, snapshots),
            config=ResolverConfig(
                max_data_age_seconds=config.get("max_age", 30),
                max_dislocation_bps=config.get("max_bps", 25),
            ),
            audit_path=None,
        )

    def resolve(self, resolver, **overrides):
        values = {
            "base_asset": "BTC",
            "quote_asset": "USD",
            "market_type": "perpetual",
            "signal_price": 100.0,
            "signal_venue": "kraken",
            "derivatives_venue": "binance_futures",
            "side": "long",
            "now": NOW,
        }
        values.update(overrides)
        return resolver.resolve(**values)

    def test_spot_instrument_is_resolved_without_derivative_fields(self):
        item = instrument("spot", "BTC/EUR", quoteAsset="EUR")
        snap = snapshot(100.0, markPrice=None, indexPrice=None)
        result = self.resolve(
            self.resolver([item], {"BTC/EUR": snap}),
            quote_asset="EUR",
            market_type="spot",
        )

        self.assertEqual(result.resolution_status, RESOLVED)
        self.assertEqual(result.instrument.market_type, "spot")
        self.assertIsNone(result.instrument.contract_multiplier)
        self.assertEqual(result.execution_price_type, "ask")

    def test_perpetual_records_full_contract_and_all_three_venues(self):
        item = instrument()
        result = self.resolve(self.resolver([item], {"XBT-USD-SWAP": snapshot()}))

        self.assertEqual(result.resolution_status, RESOLVED)
        self.assertEqual(result.signal_venue, "kraken")
        self.assertEqual(result.derivatives_venue, "binance_futures")
        self.assertEqual(result.execution_venue, "vertex")
        self.assertEqual(result.instrument.collateral_currency, "USDC")
        self.assertEqual(result.instrument.contract_multiplier, 1.0)
        self.assertEqual(result.instrument.mark_price, 100.0)
        self.assertEqual(result.instrument.index_price, 99.98)
        self.assertEqual(result.instrument.last_price, 100.0)
        self.assertEqual(result.instrument.fee_tier["taker_bps"], 4)

    def test_third_venue_and_different_symbol_are_not_inferred_from_asset(self):
        item = instrument(instrument_id="PI_XBTUSD", venue="deribit")
        catalog = MockCatalog("deribit", [item], {"PI_XBTUSD": snapshot()})
        result = self.resolve(
            ExecutionInstrumentResolver(catalog, ResolverConfig(30, 25), audit_path=None),
            execution_venue="deribit",
        )

        self.assertEqual(result.resolution_status, RESOLVED)
        self.assertEqual(result.instrument.instrument_id, "PI_XBTUSD")
        self.assertNotEqual(result.instrument.instrument_id, "BTC")
        self.assertNotIn("kraken", result.execution_venue)
        self.assertNotIn("binance", result.execution_venue)

    def test_runtime_catalog_aliases_can_match_without_a_hardcoded_xbt_rule(self):
        item = instrument(baseAsset="XBT", base_asset_aliases=["BTC"])
        result = self.resolve(self.resolver([item], {"XBT-USD-SWAP": snapshot()}))

        self.assertEqual(result.resolution_status, RESOLVED)
        self.assertEqual(result.instrument.base_asset, "XBT")

    def test_mapping_shaped_runtime_catalog_is_queried(self):
        item = instrument()
        item.pop("instrumentId")
        catalog = {
            "execution_venue": "vertex",
            "catalog_source": "coinvest_runtime",
            "instruments": {"UNPARSED-ID": item},
            "snapshots": {"UNPARSED-ID": snapshot()},
        }
        result = self.resolve(
            ExecutionInstrumentResolver(
                catalog, ResolverConfig(30, 25), audit_path=None
            )
        )

        self.assertEqual(result.resolution_status, RESOLVED)
        self.assertEqual(result.instrument.instrument_id, "UNPARSED-ID")
        self.assertEqual(result.catalog_source, "coinvest_runtime")

    def test_explicit_instrument_id_must_still_match_signal_intent(self):
        item = instrument(instrument_id="ETH-CONTRACT", baseAsset="ETH")
        result = self.resolve(
            self.resolver([item], {"ETH-CONTRACT": snapshot()}),
            instrument_id="ETH-CONTRACT",
        )

        self.assertEqual(result.resolution_status, BLOCKED)
        self.assertIn("base_asset", result.reasons[0])

    def test_live_consumers_reject_a_proposal_without_market_context(self):
        proposal = {
            "asset": "BTC", "signal": "long", "size_usd": 100,
            "target": 103, "stop_loss": 99,
        }

        with self.assertRaises(ExecutionResolutionError):
            build_execution_order(proposal)
        with self.assertRaises(ExecutionResolutionError):
            telegram_notify.send_proposal(proposal)

    def test_incomplete_derivative_metadata_is_unsupported(self):
        item = instrument()
        del item["contractMultiplier"]
        result = self.resolve(self.resolver([item], {"XBT-USD-SWAP": snapshot()}))

        self.assertEqual(result.resolution_status, UNSUPPORTED)
        self.assertFalse(result.live_trading_allowed)
        self.assertIn("contract_multiplier", result.reasons[0])

    def test_misaligned_price_blocks_resolution(self):
        result = self.resolve(
            self.resolver(
                [instrument()], {"XBT-USD-SWAP": snapshot(101.0)}, max_bps=25
            )
        )

        self.assertEqual(result.resolution_status, BLOCKED)
        self.assertGreater(result.absolute_dislocation_bps, 25)
        self.assertIn("dislocation", result.reasons[0])

    def test_stale_price_and_unknown_venue_are_blocked(self):
        stale = snapshot(observedAt=(NOW - timedelta(seconds=31)).isoformat())
        stale_result = self.resolve(
            self.resolver([instrument()], {"XBT-USD-SWAP": stale})
        )
        unknown_result = self.resolve(
            self.resolver([instrument()], {"XBT-USD-SWAP": snapshot()}, venue=None)
        )

        self.assertEqual(stale_result.resolution_status, BLOCKED)
        self.assertIn("stale", stale_result.reasons[0])
        self.assertEqual(unknown_result.resolution_status, BLOCKED)
        self.assertIn("venue is unknown", unknown_result.reasons[0])

    def test_resolution_is_audited_and_order_uses_resolved_instrument(self):
        audit = Path(__file__).parent / "data" / "test_execution_resolution.jsonl"
        audit.unlink(missing_ok=True)
        try:
            resolver = ExecutionInstrumentResolver(
                MockCatalog(
                    "vertex", [instrument()], {"XBT-USD-SWAP": snapshot()}
                ),
                ResolverConfig(30, 25),
                audit_path=audit,
            )
            proposal = resolver.resolve_proposal(
                {
                    "asset": "BTC",
                    "quote_asset": "USD",
                    "market_type": "perpetual",
                    "signal": "long",
                    "price": 100.0,
                    "entry": 100.0,
                    "target": 103.0,
                    "stop_loss": 99.0,
                    "size_usd": 100.0,
                    "leverage": 2,
                },
                signal_venue="kraken",
                derivatives_venue="binance_futures",
                now=NOW,
            )
            order = build_execution_order(proposal)
            record = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])

        finally:
            audit.unlink(missing_ok=True)
        self.assertEqual(order["symbol"], "XBT-USD-SWAP")
        self.assertEqual(proposal["entry"], 100.05)
        self.assertEqual(record["instrument"]["instrument_id"], "XBT-USD-SWAP")

    def test_risk_manager_and_telegram_consume_resolved_context(self):
        resolver = self.resolver([instrument()], {"XBT-USD-SWAP": snapshot()})
        proposal = resolver.resolve_proposal(
            {
                "strategy": "momentum-trading",
                "asset": "BTC",
                "quote_asset": "USD",
                "market_type": "perpetual",
                "signal": "long",
                "timeframe": "15m",
                "entry": 100.0,
                "price": 100.0,
                "target": 103.0,
                "stop_loss": 99.0,
                "confidence": 0.7,
                "atr": 1.0,
                "ema50": 90.0,
                "rsi": 60.0,
                "volume_ratio": 1.5,
                "new_20d_high": True,
                "leverage": 10,
            },
            signal_venue="kraken",
            derivatives_venue="binance_futures",
            now=NOW,
        )
        log_path = Path(__file__).parent / "data" / "test_execution_proposals.jsonl"
        log_path.unlink(missing_ok=True)
        try:
            risk_manager.RiskManager._LOG_PATH = log_path
            risk_manager.RiskManager._PORTFOLIO_PATH = log_path.with_suffix(".portfolio")
            validation = risk_manager.RiskManager().validate(proposal)
        finally:
            log_path.unlink(missing_ok=True)
        message = telegram_notify.format_proposal(proposal)

        self.assertTrue(validation.approved, validation.to_dict())
        self.assertIn("*Venue esecuzione:* vertex", message)
        self.assertIn("*Strumento:* XBT-USD-SWAP (perpetual)", message)
        self.assertIn("*Prezzo usato:* ask 100.05", message)


if __name__ == "__main__":
    unittest.main()
