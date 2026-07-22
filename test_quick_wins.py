import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import data_fetcher
import intraday_exit
import manage_positions
import paper_reset
import risk_manager
import sizing
import telegram_notify
import trading_mode


def resolved_market_context(price=100.0):
    return {
        "signal_venue": "kraken",
        "derivatives_venue": "binance_futures",
        "execution_venue": "test_venue",
        "resolution_status": "resolved",
        "signal_price": price,
        "signal_price_type": "last",
        "execution_price": price,
        "execution_price_type": "last",
        "execution_price_observed_at": "2026-07-22T10:00:00+00:00",
        "execution_data_age_seconds": 0.0,
        "dislocation_bps": 0.0,
        "absolute_dislocation_bps": 0.0,
        "max_data_age_seconds": 30.0,
        "max_dislocation_bps": 25.0,
        "live_trading_allowed": True,
        "instrument": {
            "execution_venue": "test_venue",
            "instrument_id": "DYNAMIC-TEST-ID",
            "base_asset": "BTC",
            "quote_asset": "USD",
            "market_type": "perpetual",
            "collateral_currency": "USD",
            "contract_multiplier": 1.0,
            "tick_size": 0.01,
            "lot_size": 0.001,
            "minimum_notional": 10.0,
            "mark_price": price,
            "index_price": price,
            "last_price": price,
            "stop_trigger_type": "mark",
            "margin_mode": "cross",
        },
    }


class RiskManagerSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="liquid-tests-"))
        risk_manager.RiskManager._LOG_PATH = self.tmp / "proposals.jsonl"
        risk_manager.RiskManager._PORTFOLIO_PATH = self.tmp / "portfolio_state.json"

    def validate(self, proposal):
        return risk_manager.RiskManager().validate(proposal).to_dict()

    def valid_proposal(self):
        return {
            "strategy": "momentum-trading",
            "asset": "BTC",
            "signal": "long",
            "timeframe": "15m",
            "entry": 100.0,
            "target": 103.0,
            "stop_loss": 99.0,
            "confidence": 0.7,
            "price": 100.0,
            "atr": 1.0,
            "ema50": 90.0,
            "rsi": 60.0,
            "volume_ratio": 1.5,
            "new_20d_high": True,
            "leverage": 10,
            "market_context": resolved_market_context(),
        }

    def test_old_claude_schema_aliases_are_normalised(self):
        proposal = {
            "strategy": "momentum_trading",
            "asset": "btc",
            "side": "long",
            "timeframe": "15m",
            "entry": 100.0,
            "tp": 103.0,
            "sl": 99.0,
            "confidence": 0.7,
            "price": 100.0,
            "atr": 1.0,
            "ema50": 90.0,
            "rsi": 60.0,
            "vol_ratio": 1.5,
            "new_20d_high": "true",
            "leverage": 10,
            "market_context": resolved_market_context(),
        }

        result = self.validate(proposal)

        self.assertTrue(result["approved"], result)
        self.assertEqual(result["validated"]["strategy"], "momentum-trading")
        self.assertEqual(result["validated"]["asset"], "BTC")
        self.assertEqual(result["validated"]["signal"], "long")
        self.assertEqual(result["validated"]["target"], 103.0)
        self.assertEqual(result["validated"]["stop_loss"], 99.0)

    def test_invalid_enums_are_rejected_not_approved(self):
        cases = [
            ("signal", {**self.valid_proposal(), "signal": "buy"}, "Invalid signal"),
            ("timeframe", {**self.valid_proposal(), "timeframe": "4h"}, "Invalid timeframe"),
            ("asset", {**self.valid_proposal(), "asset": "DOGE"}, "Invalid asset"),
        ]
        for name, proposal, reason in cases:
            with self.subTest(name=name):
                result = self.validate(proposal)
                self.assertFalse(result["approved"], result)
                self.assertIn(reason, result["rejection_reason"])

    def test_missing_fields_return_structured_rejection(self):
        result = self.validate({"strategy": "momentum-trading", "asset": "BTC"})

        self.assertFalse(result["approved"], result)
        self.assertIn("Missing required proposal field", result["rejection_reason"])


class DataFetcherValidationTests(unittest.TestCase):
    def complete_output(self):
        indicators = {key: 1.0 for key in data_fetcher.REQUIRED_INDICATOR_KEYS}
        return {
            "assets": {
                asset: {
                    "live": {"price": 100.0},
                    "timeframes": {
                        "15m": [{"close": 100.0, "is_final": True, "available_at": 0}],
                        "1h": [{"close": 100.0, "is_final": True, "available_at": 0}],
                    },
                    "indicators": {
                        "15m": dict(indicators),
                        "1h": dict(indicators),
                    },
                }
                for asset in data_fetcher.ASSETS
            }
        }

    def test_signal_ready_output_passes_when_required_data_exists(self):
        data_fetcher.validate_signal_ready_output(self.complete_output())

    def test_signal_ready_output_fails_on_missing_intraday_candles(self):
        output = self.complete_output()
        output["assets"]["BTC"]["timeframes"]["15m"] = []

        with self.assertRaisesRegex(RuntimeError, "BTC 15m: candles mancanti"):
            data_fetcher.validate_signal_ready_output(output)


class AdaptiveSizingTests(unittest.TestCase):
    def fit(self, **overrides):
        params = {
            "equity": 100.0,
            "available_balance": 100.0,
            "risk_pct": 0.04,
            "entry": 100.0,
            "stop_loss": 99.0,
            "leverage": 1.0,
            "max_leverage": 10.0,
            "max_total_margin_pct": 0.30,
            "max_per_asset_margin_pct": 0.30,
        }
        params.update(overrides)
        return sizing.fit_leverage_to_margin(**params)

    def test_ideal_size_that_fits_is_not_reduced(self):
        result = self.fit(risk_pct=0.02, max_total_margin_pct=0.40,
                          max_per_asset_margin_pct=0.40)

        self.assertTrue(result["fits"], result)
        self.assertFalse(result["size_adjusted"])
        self.assertEqual(result["risk_pct"], result["target_risk_pct"])
        self.assertEqual(result["risk_retention"], 1.0)

    def test_reduced_size_at_exactly_75_percent_is_accepted(self):
        result = self.fit()

        self.assertTrue(result["fits"], result)
        self.assertTrue(result["size_adjusted"])
        self.assertAlmostEqual(result["risk_pct"], 0.03, places=4)
        self.assertAlmostEqual(result["target_risk_pct"], 0.04, places=4)
        self.assertAlmostEqual(result["risk_retention"], 0.75, places=4)
        self.assertLessEqual(result["margin_usd"], result["margin_budget"])

    def test_reduced_size_below_75_percent_is_rejected(self):
        result = self.fit(max_total_margin_pct=0.299,
                          max_per_asset_margin_pct=0.299)

        self.assertFalse(result["fits"], result)
        self.assertTrue(result["size_adjusted"])
        self.assertLess(result["risk_retention"], 0.75)
        self.assertIn("richiesto almeno 75%", result["reason"])

    def test_absolute_075_percent_floor_is_enforced(self):
        result = self.fit(
            risk_pct=0.008,
            max_total_margin_pct=0.07,
            max_per_asset_margin_pct=0.07,
        )

        self.assertFalse(result["fits"], result)
        self.assertAlmostEqual(result["minimum_risk_pct"], 0.0075, places=4)
        self.assertAlmostEqual(result["risk_pct"], 0.007, places=4)

    def test_previous_btc_setup_is_accepted_with_reduced_risk(self):
        result = sizing.fit_leverage_to_margin(
            equity=154.92,
            available_balance=154.92,
            risk_pct=0.03,
            entry=66820.7,
            stop_loss=66600.0,
            leverage=10.0,
        )

        self.assertTrue(result["fits"], result)
        self.assertTrue(result["size_adjusted"])
        self.assertAlmostEqual(result["risk_pct"], 0.0264, places=4)
        self.assertGreaterEqual(result["risk_retention"], 0.75)
        self.assertLessEqual(result["margin_usd"], result["margin_budget"])


class TelegramNotifyTests(unittest.TestCase):
    def test_format_proposal_displays_strategy_without_markdown_underscore(self):
        msg = telegram_notify.format_proposal({
            "strategy": "momentum_trading",
            "asset": "BTC",
            "signal": "long",
            "timeframe": "15m",
            "entry": 100,
            "target": 103,
            "stop_loss": 99,
            "leverage": 10,
            "confidence": 0.7,
            "risk_reward": 2.0,
        })

        self.assertIn("momentum-trading", msg)
        self.assertNotIn("momentum_trading", msg)

    def test_format_proposal_displays_adapted_risk(self):
        msg = telegram_notify.format_proposal({
            "strategy": "momentum-trading",
            "asset": "BTC",
            "signal": "long",
            "timeframe": "15m",
            "entry": 66820.7,
            "target": 67350.0,
            "stop_loss": 66600.0,
            "leverage": 20,
            "requested_leverage": 10,
            "confidence": 0.64,
            "risk_reward": 2.4,
            "equity": 154.92,
            "size_usd": 1239.36,
            "margin_usd": 61.97,
            "risk_pct": 0.0264,
            "risk_usd": 4.09,
            "target_risk_pct": 0.03,
            "target_risk_usd": 4.65,
            "risk_retention": 0.88,
            "size_adjusted": True,
            "adjusted": True,
        })

        self.assertIn("Rischio se tocca SL (stima):* $4.09 (2.6% equity)", msg)
        self.assertIn("rischio ideale 3.00% / $4.65", msg)
        self.assertIn("effettivo 2.64% (88.0% conservato)", msg)

    def test_notify_and_wait_uses_offset_captured_before_send(self):
        calls = {}

        def fake_send(token, chat_id, text):
            calls["sent"] = (token, chat_id, text)

        def fake_wait(timeout_minutes=30, start_offset=None):
            calls["wait"] = (timeout_minutes, start_offset)
            return True

        with patch.object(telegram_notify, "_creds", return_value=("token", "chat")):
            with patch.object(telegram_notify, "_latest_offset", return_value=123):
                with patch.object(telegram_notify, "_do_send", side_effect=fake_send):
                    with patch.object(telegram_notify, "wait_response", side_effect=fake_wait):
                        ok = telegram_notify.notify_and_wait(
                            {
                                "asset": "BTC",
                                "signal": "long",
                                "confidence": 0.7,
                                "market_context": resolved_market_context(),
                            },
                            timeout_minutes=7,
                        )

        self.assertTrue(ok)
        self.assertEqual(calls["wait"], (7, 123))
        self.assertIn("BTC", calls["sent"][2])


class ManagePositionsTests(unittest.TestCase):
    def now(self):
        return datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    def position(self, **overrides):
        data = {
            "asset": "BTC",
            "symbol": "BTC-PERP",
            "side": "long",
            "size_coin": 0.1,
            "entry_price": 100.0,
            "mark_price": 110.0,
            "tp": 120.0,
            "sl": 95.0,
            "opened_at": (self.now() - timedelta(hours=1)).isoformat(),
        }
        data.update(overrides)
        return data

    def test_close_when_very_close_to_tp(self):
        actions = manage_positions.actions_for_position(
            self.position(mark_price=119.0),
            now=self.now(),
        )

        self.assertEqual(actions[0]["action"], "close")
        self.assertAlmostEqual(actions[0]["progress"], 0.95)

    def test_close_stale_trade_with_low_progress(self):
        actions = manage_positions.actions_for_position(
            self.position(
                mark_price=104.0,
                opened_at=(self.now() - timedelta(hours=3, minutes=30)).isoformat(),
            ),
            now=self.now(),
        )

        self.assertEqual(actions[0]["action"], "close")
        self.assertIn("Trade fermo", actions[0]["reason"])

    def test_max_hold_does_not_close_when_progress_is_good(self):
        actions = manage_positions.actions_for_position(
            self.position(
                mark_price=112.0,
                opened_at=(self.now() - timedelta(hours=7)).isoformat(),
            ),
            now=self.now(),
        )

        self.assertTrue(actions)
        self.assertEqual(actions[0]["action"], "modify_sl")

    def test_missing_runtime_instrument_never_falls_back_to_asset(self):
        actions = manage_positions.actions_for_position(
            self.position(symbol=None),
            now=self.now(),
        )

        self.assertEqual(actions[0]["status"], "unsupported")
        self.assertIsNone(actions[0]["symbol"])


class IntradayExitTests(unittest.TestCase):
    def test_no_legacy_max_hold_flatten_by_default(self):
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        snapshot = {
            "positions": [{
                "asset": "BTC",
                "symbol": "BTC-PERP",
                "side": "long",
                "opened_at": (now - timedelta(hours=10)).isoformat(),
            }]
        }

        self.assertEqual(intraday_exit.positions_to_flatten(snapshot, now=now), [])

    def test_end_of_day_still_flattens_all_positions(self):
        now = datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc)
        snapshot = {
            "positions": [{
                "asset": "BTC",
                "symbol": "BTC-PERP",
                "side": "long",
            }]
        }

        actions = intraday_exit.positions_to_flatten(snapshot, now=now)
        self.assertEqual(actions[0]["symbol"], "BTC-PERP")
        self.assertIn("end-of-day", actions[0]["reason"])

    def test_end_of_day_missing_instrument_is_unsupported_not_asset(self):
        now = datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc)
        snapshot = {
            "positions": [{
                "asset": "BTC",
                "side": "long",
            }]
        }

        actions = intraday_exit.positions_to_flatten(snapshot, now=now)

        self.assertEqual(actions[0]["status"], "unsupported")
        self.assertIsNone(actions[0]["symbol"])
        self.assertNotEqual(actions[0]["symbol"], actions[0]["asset"])


class TradingModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="liquid-mode-tests-"))
        self.old_request_path = paper_reset.REQUEST_PATH
        self.old_env_path = trading_mode.ENV_PATH
        paper_reset.REQUEST_PATH = self.tmp / "reset_request.json"
        trading_mode.ENV_PATH = self.tmp / "missing.env"

    def tearDown(self):
        paper_reset.REQUEST_PATH = self.old_request_path
        trading_mode.ENV_PATH = self.old_env_path

    def test_default_mode_is_paper_and_live_actions_are_blocked(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(trading_mode.trading_mode(), "paper")
            with self.assertRaises(trading_mode.TradingModeError):
                trading_mode.require_live_trading_enabled("test ordine")

    def test_live_requires_second_switch(self):
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=True):
            with self.assertRaises(trading_mode.TradingModeError):
                trading_mode.require_live_trading_enabled("test ordine")

        with patch.dict(
            os.environ,
            {"TRADING_MODE": "live", "LIVE_TRADING_ALLOWED": "true"},
            clear=True,
        ):
            self.assertTrue(trading_mode.require_live_trading_enabled("test ordine"))

    def test_paper_reset_is_disabled_in_live_mode(self):
        with patch.dict(
            os.environ,
            {"TRADING_MODE": "live", "LIVE_TRADING_ALLOWED": "true"},
            clear=True,
        ):
            with self.assertRaises(trading_mode.TradingModeError):
                paper_reset.request_reset(requested_by="test")
            paper_reset.REQUEST_PATH.parent.mkdir(exist_ok=True)
            paper_reset.REQUEST_PATH.write_text("{}", encoding="utf-8")
            self.assertIsNone(paper_reset.pending())


if __name__ == "__main__":
    unittest.main()
