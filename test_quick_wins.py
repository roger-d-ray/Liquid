import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_fetcher
import risk_manager
import telegram_notify


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
                        "15m": [{"close": 100.0}],
                        "1h": [{"close": 100.0}],
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


class TelegramNotifyTests(unittest.TestCase):
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
                            {"asset": "BTC", "signal": "long", "confidence": 0.7},
                            timeout_minutes=7,
                        )

        self.assertTrue(ok)
        self.assertEqual(calls["wait"], (7, 123))
        self.assertIn("BTC", calls["sent"][2])


if __name__ == "__main__":
    unittest.main()
