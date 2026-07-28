"""Regression tests for propose_pipeline.run_pipeline().

The pipeline collapses resolve → validate → size → notify into one process so the
four separate execution-freshness checks all evaluate within the same sub-second
window (the 30s gate itself is NOT weakened). These tests drive the full chain in
`dry_run=True` (no Telegram) and assert the three terminal outcomes:

  * FIT      → chain completes, sizing injected, exit 0
  * DISCARD  → sizing risk floor rejects (tiny account + tight stop), exit 20
  * STALE    → old book blocks at resolve, no notify, exit 40
"""

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import propose_pipeline as pp
import risk_manager


def _build_input(book_time_ms, equity, stop_loss, target):
    """A momentum-short ETH proposal that passes the risk validator, plus the
    three Co-Invest payloads the adapter needs to prove one market identity."""
    return {
        "proposal": {
            "strategy": "momentum_trading", "asset": "ETH", "side": "short",
            "timeframe": "1h", "entry": 1919.5, "tp": target, "sl": stop_loss,
            "leverage": 10, "risk_pct": 0.03, "confidence": 0.62,
            "signal_price": 1919.5, "price": 1919.5,
            "ema50": 1930.0, "ema200": 1945.0, "rsi": 31.0, "atr": 6.0,
            "macd_histogram": -0.8, "volume_ratio": 1.0, "new_20d_low": False,
            "motivation": "Downtrend 1h.",
        },
        "coinvest": {
            "search_markets": {"structuredContent": {
                "text": "1 markets matching ETH:\n1. ETH | Max 25x"}},
            "analyze_market": {"structuredContent": {
                "symbol": "ETH",
                "ticker": {"coin": "ETH", "markPx": "1919.9", "maxLeverage": 25}}},
            "show_orderbook": {"structuredContent": {"book": {
                "coin": "ETH", "displayName": "ETH",
                "bids": [{"px": "1919.5", "sz": "195.1", "n": 42}],
                "asks": [{"px": "1919.6", "sz": "25.2", "n": 8}],
                "time": book_time_ms}}},
        },
        "venues": {
            "signal_venue": "kraken", "derivatives_venue": "binance_futures",
            "execution_venue": "liquid",
        },
        "received_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": {
            "total_equity": equity, "available_balance": equity, "positions": [],
        },
    }


class ProposePipelineTests(unittest.TestCase):
    def setUp(self):
        # Redirect risk_manager's proposal log to a temp file so tests never
        # pollute the real logs/proposals.jsonl (mirrors test_quick_wins).
        self.tmp = Path(tempfile.mkdtemp(prefix="liquid-pipe-tests-"))
        self._saved_log_path = risk_manager.RiskManager._LOG_PATH
        risk_manager.RiskManager._LOG_PATH = self.tmp / "proposals.jsonl"

    def tearDown(self):
        risk_manager.RiskManager._LOG_PATH = self._saved_log_path

    def _run(self, payload):
        # audit_path=None disables the resolver's execution_resolution.jsonl log.
        return pp.run_pipeline(
            payload, dry_run=True, timeout_minutes=1, audit_path=None,
        )

    def test_fit_completes_the_chain(self):
        # Wide (~1%) stop + a $10k account: the ideal risk-based notional fits
        # inside the 40% per-asset margin cap, so the whole chain succeeds.
        payload = _build_input(int(time.time() * 1000), 10_000.0, 1939.0, 1880.5)
        result, code = self._run(payload)
        self.assertEqual(code, 0)
        self.assertEqual(result["stage"], "size")
        self.assertTrue(result["ok"])
        self.assertTrue(result["fits"])
        prop = result["proposal"]
        # Entry is the executable bid, sizing fields are injected, risk = 3%.
        self.assertEqual(prop["entry"], 1919.5)
        self.assertGreater(prop["size_usd"], 0)
        self.assertAlmostEqual(prop["risk_usd"], 300.0, places=2)

    def test_tiny_account_tight_stop_is_discarded(self):
        # $127 account + a very tight (~0.23%) stop: the margin-capped max size
        # can't retain enough risk → sizing floor discards it (no crash).
        payload = _build_input(int(time.time() * 1000), 126.91, 1924.0, 1910.0)
        result, code = self._run(payload)
        self.assertEqual(code, 20)
        self.assertEqual(result["stage"], "size")
        self.assertFalse(result["fits"])
        self.assertIn("reason", result)
        # dry_run must never fire a Telegram discard message.
        self.assertFalse(result["discard_notified"])

    def test_stale_book_blocks_at_resolve(self):
        # A book from days ago: the 30s freshness gate must still block it before
        # any notification — proof the pipeline does not weaken the gate.
        payload = _build_input(1_784_726_556_178, 10_000.0, 1939.0, 1880.5)
        result, code = self._run(payload)
        self.assertEqual(code, 40)
        self.assertEqual(result["stage"], "resolve")
        self.assertEqual(result["resolution_status"], "blocked")
        self.assertTrue(any("stale" in str(r) for r in result["reasons"]))

    def test_bad_identity_is_unsupported_no_notify(self):
        # analyze_market says ETH but the order book says BTC → identity is not
        # proven; the adapter must refuse and the pipeline returns no-trade.
        payload = _build_input(int(time.time() * 1000), 10_000.0, 1939.0, 1880.5)
        payload["coinvest"]["show_orderbook"]["structuredContent"]["book"]["coin"] = "BTC"
        result, code = self._run(payload)
        self.assertEqual(code, 40)
        self.assertEqual(result["stage"], "adapter")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
