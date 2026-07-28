import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import data_fetcher
import market_summary


def bar(index):
    return {
        "open_time": index * 900_000,
        "close_time": (index + 1) * 900_000,
        "open": 100.0 + index,
        "high": 102.0 + index,
        "low": 99.0 + index,
        "close": 101.0 + index,
        "volume": 10.0 + index,
        "source": "test",
        "received_at": (index + 1) * 900_000,
        "available_at": (index + 1) * 900_000,
        "aggregation_method": "native",
        "completeness": "complete",
        "quality_flags": [],
        "component_count": 1,
        "expected_component_count": 1,
        "missing_component_open_times": [],
        "is_final": True,
    }


def indicator_snapshot():
    return {
        "ema9": 101.0,
        "ema21": 100.0,
        "ema50": 99.0,
        "ema200": 95.0,
        "rsi": 60.0,
        "macd": {"macd": 2.0, "signal": 1.5, "histogram": 0.5},
        "bollinger": {"upper": 110.0, "middle": 100.0, "lower": 90.0},
        "stochastic": {"k": 70.0, "d": 65.0},
        "atr": 2.0,
        "adx": 30.0,
        "plus_di": 25.0,
        "minus_di": 15.0,
        "vol_sma20": 12.0,
        "vol_ratio": 1.25,
        "swing_highs": [105.0],
        "swing_lows": [95.0],
        "resistance": 105.0,
        "support": 95.0,
        "high_20_bars": 106.0,
        "low_20_bars": 94.0,
        "high_55_bars": 110.0,
        "low_55_bars": 90.0,
    }


def complete_market_data():
    bars = [bar(index) for index in range(10)]
    return {
        "schema_version": data_fetcher.MARKET_DATA_SCHEMA_VERSION,
        "timestamp": "2026-07-28T15:57:53+00:00",
        "assets": {
            asset: {
                "timeframes": {
                    timeframe: list(bars)
                    for timeframe in ("15m", "1h", "4h", "1d")
                },
                "intrabar": {},
                "incomplete": {},
                "indicators": {
                    timeframe: indicator_snapshot()
                    for timeframe in ("15m", "1h", "4h", "1d")
                },
                "live": {
                    "price": 110.0,
                    "source": "test",
                    "signal_venue": "test",
                    "quality_flags": [],
                },
                "news": [],
                "unusual_activity": [],
            }
            for asset in data_fetcher.ASSETS
        },
    }


class MarketSummaryTests(unittest.TestCase):
    def test_builds_compact_signal_and_context_sections(self):
        summary = market_summary.build_market_summary(
            complete_market_data(), recent_bars=2, context_bars=1
        )

        btc = summary["assets"]["BTC"]
        signal = btc["signal_timeframes"]["15m"]
        context = btc["context_timeframes"]["4h"]
        self.assertEqual(signal["closed_bar_count"], 10)
        self.assertEqual(len(signal["recent_closed_bars"]), 2)
        self.assertEqual(len(context["recent_closed_bars"]), 1)
        self.assertEqual(signal["indicators"]["volume_ratio"], 1.25)
        self.assertEqual(signal["indicators"]["macd_histogram"], 0.5)
        self.assertNotIn("intrabar", btc)
        self.assertNotIn("incomplete", btc)

    def test_rejects_unsupported_schema_before_analysis(self):
        data = complete_market_data()
        data["schema_version"] = 1

        with self.assertRaisesRegex(
            market_summary.MarketSummaryError, "schema market_data non supportato"
        ):
            market_summary.build_market_summary(data)

    def test_reports_wrong_timeframes_shape_without_attribute_error(self):
        data = complete_market_data()
        data["assets"]["BTC"]["timeframes"] = []

        with self.assertRaisesRegex(
            market_summary.MarketSummaryError,
            r"assets\.BTC\.timeframes deve essere un oggetto JSON",
        ):
            market_summary.build_market_summary(data)

    def test_missing_macro_context_is_warning_not_signal_failure(self):
        data = complete_market_data()
        data["assets"]["BTC"]["timeframes"]["4h"] = []
        data["assets"]["BTC"]["indicators"]["4h"] = {}

        summary = market_summary.build_market_summary(data)

        btc = summary["assets"]["BTC"]
        self.assertNotIn("4h", btc["context_timeframes"])
        self.assertIn(
            "BTC 4h: contesto barre non disponibile",
            btc["warnings"],
        )

    def test_cli_prints_parseable_json_without_inline_python(self):
        with tempfile.TemporaryDirectory(prefix="liquid-summary-") as tmp:
            input_path = Path(tmp) / "market_data.json"
            input_path.write_text(
                json.dumps(complete_market_data()),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = market_summary.main(
                    ["--input", str(input_path), "--recent-bars", "2"]
                )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered["schema_version"], 2)
        self.assertEqual(
            len(
                rendered["assets"]["SOL"]["signal_timeframes"]["1h"][
                    "recent_closed_bars"
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
