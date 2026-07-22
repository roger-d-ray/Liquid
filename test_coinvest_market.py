import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import risk_manager
import sizing
from coinvest_market import CoinvestPayloadError, CoinvestRuntimeAdapter
from execution_instrument import (
    BLOCKED,
    RESOLVED,
    UNSUPPORTED,
    ExecutionInstrumentResolver,
    ExecutionResolutionError,
    ResolverConfig,
    build_execution_order,
)


FIXTURE_PATH = (
    Path(__file__).parent / "test_fixtures" / "coinvest_eth_runtime.json"
)


def fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def captured_at(data):
    return datetime.fromisoformat(data["captured_at"].replace("Z", "+00:00"))


def adapter(data, *, received_at=None):
    return CoinvestRuntimeAdapter(
        data["search_markets"],
        data["analyze_market"],
        data["show_orderbook"],
        received_at=received_at or captured_at(data),
    )


def resolve(data, *, side="long", now=None, signal_price=1919.55):
    observed = datetime.fromtimestamp(
        data["show_orderbook"]["structuredContent"]["book"]["time"] / 1000,
        tz=timezone.utc,
    )
    resolver = ExecutionInstrumentResolver(
        adapter(data),
        ResolverConfig(
            max_data_age_seconds=30,
            max_dislocation_bps=25,
            minimum_collateral_usd=15,
        ),
        audit_path=None,
    )
    return resolver.resolve(
        base_asset="ETH",
        signal_price=signal_price,
        signal_venue="kraken",
        derivatives_venue="binance_futures",
        side=side,
        now=now or observed + timedelta(seconds=1),
    )


class CoinvestRuntimeAdapterTests(unittest.TestCase):
    def test_real_payload_shape_resolves_without_unexposed_exchange_metadata(self):
        context = resolve(fixture())

        self.assertEqual(context.resolution_status, RESOLVED)
        self.assertEqual(context.execution_venue, "liquid")
        self.assertEqual(context.instrument.instrument_id, "ETH")
        self.assertEqual(context.instrument.base_asset, "ETH")
        self.assertEqual(context.instrument.maximum_leverage, 25)
        self.assertEqual(context.execution_price_type, "ask")
        self.assertEqual(context.execution_price, 1919.6)
        self.assertIsNone(context.instrument.quote_asset)
        self.assertIsNone(context.instrument.market_type)
        self.assertIsNone(context.instrument.tick_size)
        self.assertIsNone(context.instrument.lot_size)
        self.assertIsNone(context.instrument.minimum_notional)

    def test_short_uses_best_bid_and_unsorted_levels_are_normalized(self):
        data = fixture()
        book = data["show_orderbook"]["structuredContent"]["book"]
        book["bids"].reverse()
        book["asks"].reverse()

        context = resolve(data, side="short")

        self.assertEqual(context.resolution_status, RESOLVED)
        self.assertEqual(context.execution_price_type, "bid")
        self.assertEqual(context.execution_price, 1919.5)

    def test_serialized_runtime_catalog_handoff_remains_resolvable(self):
        data = fixture()
        observed = datetime.fromtimestamp(
            data["show_orderbook"]["structuredContent"]["book"]["time"] / 1000,
            tz=timezone.utc,
        )
        catalog = adapter(data).to_runtime_catalog()
        resolver = ExecutionInstrumentResolver(
            catalog, ResolverConfig(30, 25, 15), audit_path=None
        )

        context = resolver.resolve(
            base_asset="ETH",
            signal_price=1919.55,
            signal_venue="kraken",
            derivatives_venue="binance_futures",
            side="long",
            now=observed + timedelta(seconds=1),
        )

        self.assertEqual(context.resolution_status, RESOLVED)
        self.assertEqual(context.instrument.instrument_id, "ETH")

    def test_symbol_mismatch_is_never_inferred_or_parsed(self):
        data = fixture()
        data["show_orderbook"]["structuredContent"]["book"]["coin"] = "ETHFI"

        with self.assertRaisesRegex(CoinvestPayloadError, "conflicts"):
            adapter(data)

    def test_search_text_is_evidence_not_a_machine_catalog(self):
        data = fixture()
        data["search_markets"]["structuredContent"]["text"] = (
            "Ambiguous human text containing ETH and ETHFI"
        )
        data["analyze_market"]["structuredContent"]["type"] = "crypto"

        context = resolve(data)

        self.assertEqual(context.resolution_status, RESOLVED)
        self.assertIsNone(context.instrument.market_type)

    def test_missing_book_side_without_typed_last_trade_is_unsupported(self):
        data = fixture()
        data["show_orderbook"]["structuredContent"]["book"]["asks"] = []

        context = resolve(data, side="long")

        self.assertEqual(context.resolution_status, UNSUPPORTED)
        self.assertIn("neither", context.reasons[0])

    def test_missing_server_time_uses_explicit_client_receipt_time(self):
        data = fixture()
        data["show_orderbook"]["structuredContent"]["book"].pop("time")
        received = captured_at(data)
        provider = adapter(data, received_at=received)
        resolver = ExecutionInstrumentResolver(
            provider, ResolverConfig(30, 25, 15), audit_path=None
        )

        context = resolver.resolve(
            base_asset="ETH",
            signal_price=1919.55,
            signal_venue="kraken",
            derivatives_venue="binance_futures",
            side="long",
            now=received + timedelta(seconds=1),
        )

        self.assertEqual(context.resolution_status, RESOLVED)
        self.assertEqual(
            context.execution_price_observed_at, received.isoformat()
        )

    def test_stale_book_and_excessive_dislocation_still_block(self):
        data = fixture()
        observed = datetime.fromtimestamp(
            data["show_orderbook"]["structuredContent"]["book"]["time"] / 1000,
            tz=timezone.utc,
        )

        stale = resolve(data, now=observed + timedelta(seconds=31))
        displaced = resolve(data, signal_price=1900.0)

        self.assertEqual(stale.resolution_status, BLOCKED)
        self.assertIn("stale", stale.reasons[0])
        self.assertEqual(displaced.resolution_status, BLOCKED)
        self.assertIn("dislocation", displaced.reasons[0])


class CoinvestExecutionContractTests(unittest.TestCase):
    def executable_proposal(self, *, size_usd, leverage=10, minimum_notional=None):
        data = fixture()
        current = datetime.now(timezone.utc).replace(microsecond=0)
        data["captured_at"] = current.isoformat()
        data["show_orderbook"]["structuredContent"]["book"]["time"] = int(
            current.timestamp() * 1000
        )
        context = resolve(data, now=current, signal_price=1919.55).to_dict()
        context["instrument"]["minimum_notional"] = minimum_notional
        return {
            "asset": "ETH",
            "signal": "long",
            "entry": context["execution_price"],
            "target": 1977.188,
            "stop_loss": 1900.404,
            "size_usd": size_usd,
            "leverage": leverage,
            "market_context": context,
        }

    def test_collateral_boundary_is_enforced_by_order_builder(self):
        with self.assertRaisesRegex(ExecutionResolutionError, "collateral"):
            build_execution_order(
                self.executable_proposal(size_usd=149.99),
            )

        order = build_execution_order(self.executable_proposal(size_usd=150.0))
        self.assertEqual(order["symbol"], "ETH")
        self.assertEqual(order["size"], 150.0)
        self.assertEqual(order["leverage"], 10.0)

    def test_authoritative_minimum_notional_is_enforced_separately(self):
        proposal = self.executable_proposal(
            size_usd=90.0, leverage=5, minimum_notional=100.0
        )

        with self.assertRaisesRegex(ExecutionResolutionError, "minimum_notional"):
            build_execution_order(proposal)

    def test_sizing_respects_connector_leverage_and_collateral(self):
        proposal = self.executable_proposal(size_usd=150.0)
        proposal.update(risk_pct=0.03)
        plan = sizing.plan_trade(
            proposal,
            {
                "total_equity": 1000.0,
                "available_balance": 1000.0,
                "positions": [],
            },
        )

        self.assertTrue(plan["fits"], plan)
        self.assertLessEqual(plan["leverage"], 20)
        self.assertGreaterEqual(plan["margin_usd"], 15)

    def test_real_payload_shape_passes_risk_sizing_and_order_build(self):
        proposal = self.executable_proposal(size_usd=150.0)
        proposal.update(
            strategy="momentum-trading",
            timeframe="15m",
            confidence=0.70,
            price=proposal["entry"],
            atr=19.196,
            ema50=1800.0,
            rsi=60.0,
            volume_ratio=1.5,
            new_20d_high=True,
            risk_pct=0.03,
        )
        log_path = Path(__file__).parent / "data" / "test_coinvest_risk.jsonl"
        portfolio_path = log_path.with_suffix(".portfolio.json")
        old_log = risk_manager.RiskManager._LOG_PATH
        old_portfolio = risk_manager.RiskManager._PORTFOLIO_PATH
        try:
            risk_manager.RiskManager._LOG_PATH = log_path
            risk_manager.RiskManager._PORTFOLIO_PATH = portfolio_path
            validation = risk_manager.RiskManager().validate(proposal)
        finally:
            risk_manager.RiskManager._LOG_PATH = old_log
            risk_manager.RiskManager._PORTFOLIO_PATH = old_portfolio
            log_path.unlink(missing_ok=True)

        self.assertTrue(validation.approved, validation.to_dict())
        plan = sizing.plan_trade(
            proposal,
            {
                "total_equity": 1000.0,
                "available_balance": 1000.0,
                "positions": [],
            },
        )
        self.assertTrue(plan["fits"], plan)
        executable = {**proposal, **plan}
        order = build_execution_order(executable)
        self.assertEqual(order["symbol"], "ETH")

    def test_sizing_rejects_when_margin_budget_is_below_platform_floor(self):
        result = sizing.fit_leverage_to_margin(
            equity=100,
            available_balance=14.99,
            risk_pct=0.03,
            entry=100,
            stop_loss=99,
            leverage=10,
            max_total_margin_pct=1,
            max_per_asset_margin_pct=1,
        )

        self.assertFalse(result["fits"])
        self.assertIn("collaterale minimo", result["reason"])

    def test_live_consumers_recompute_freshness(self):
        proposal = self.executable_proposal(size_usd=150.0)
        observed = datetime.fromisoformat(
            proposal["market_context"]["execution_price_observed_at"]
        )

        with self.assertRaisesRegex(ExecutionResolutionError, "stale"):
            build_execution_order(
                proposal,
                now=observed + timedelta(seconds=31),
            )


if __name__ == "__main__":
    unittest.main()
