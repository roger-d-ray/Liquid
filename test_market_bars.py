import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import data_fetcher
from market_bars import ClosedBar, normalize_ohlcv, utc_ms


UTC = timezone.utc


def row(open_time, close=101.0, volume=10.0):
    return {
        "open_time": open_time,
        "open": 100.0,
        "high": max(102.0, close),
        "low": min(99.0, close),
        "close": close,
        "volume": volume,
    }


class BarFinalityTests(unittest.TestCase):
    def setUp(self):
        self.open_time = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    def normalise(self, rows, now, received=None, existing=()):
        return normalize_ohlcv(
            rows,
            15 * 60,
            "kraken",
            observed_at=now,
            received_at=received or now,
            grace_period_seconds=2,
            existing_closed=existing,
        )

    def test_request_few_seconds_before_close_is_intrabar(self):
        batch = self.normalise(
            [row(self.open_time)],
            self.open_time + timedelta(minutes=14, seconds=57),
        )
        self.assertEqual(batch.closed, ())
        self.assertEqual(batch.intrabar[0].bar_open_time, utc_ms(self.open_time))
        self.assertFalse(batch.intrabar[0].is_final)

    def test_request_exactly_at_close_is_still_intrabar_during_grace(self):
        batch = self.normalise([row(self.open_time)], self.open_time + timedelta(minutes=15))
        self.assertEqual(batch.closed, ())
        self.assertEqual(len(batch.intrabar), 1)

    def test_delayed_arrival_controls_available_at(self):
        observed = self.open_time + timedelta(minutes=15, seconds=10)
        received = self.open_time + timedelta(minutes=15, seconds=12)
        bar = self.normalise([row(self.open_time)], observed, received).closed[0]
        self.assertEqual(bar.close_time, utc_ms(self.open_time + timedelta(minutes=15)))
        self.assertEqual(bar.available_at, utc_ms(received))
        self.assertEqual(bar.received_at, utc_ms(received))

    def test_last_response_row_can_be_incomplete(self):
        previous = self.open_time - timedelta(minutes=15)
        now = self.open_time + timedelta(minutes=10)
        batch = self.normalise([row(previous), row(self.open_time)], now)
        self.assertEqual([b.open_time for b in batch.closed], [utc_ms(previous)])
        self.assertEqual([b.bar_open_time for b in batch.intrabar], [utc_ms(self.open_time)])

    def test_duplicate_snapshot_updates_but_closed_bar_is_immutable(self):
        before_close = self.open_time + timedelta(minutes=10)
        snapshots = self.normalise(
            [row(self.open_time, close=101), row(self.open_time, close=102, volume=12)],
            before_close,
        )
        self.assertEqual(len(snapshots.intrabar), 1)
        self.assertEqual(snapshots.intrabar[0].close, 102)

        original = ClosedBar(
            open_time=utc_ms(self.open_time),
            close_time=utc_ms(self.open_time + timedelta(minutes=15)),
            open=100, high=102, low=99, close=101, volume=10,
            source="kraken", received_at=utc_ms(before_close),
            available_at=utc_ms(before_close),
        )
        after_close = self.open_time + timedelta(minutes=16)
        merged = self.normalise([row(self.open_time, close=103)], after_close, existing=(original,))
        self.assertEqual(merged.closed[0], original)
        self.assertEqual(merged.intrabar, ())

    def test_future_and_out_of_sequence_timestamps_are_rejected(self):
        now = self.open_time + timedelta(minutes=5)
        with self.assertRaisesRegex(ValueError, "future candle"):
            self.normalise([row(now + timedelta(seconds=1))], now)
        with self.assertRaisesRegex(ValueError, "out-of-sequence"):
            self.normalise([row(self.open_time), row(self.open_time - timedelta(minutes=15))], now)

    def test_utc_day_boundary_is_independent_of_input_timezone(self):
        rome = timezone(timedelta(hours=2))
        local_midnight_bar = datetime(2026, 7, 23, 2, 0, tzinfo=rome)
        now = datetime(2026, 7, 24, 0, 0, 3, tzinfo=UTC)
        bar = normalize_ohlcv(
            [row(local_midnight_bar)], 86400, "kraken",
            observed_at=now, received_at=now, grace_period_seconds=2,
        ).closed[0]
        self.assertEqual(bar.open_time, utc_ms(datetime(2026, 7, 23, tzinfo=UTC)))
        self.assertEqual(bar.close_time, utc_ms(datetime(2026, 7, 24, tzinfo=UTC)))


class ClosedBarConsumerTests(unittest.TestCase):
    def test_indicators_ignore_intrabar_and_not_yet_available_bars(self):
        start = datetime(2026, 7, 20, tzinfo=UTC)
        candles = []
        for i in range(20):
            opened = start + timedelta(minutes=15 * i)
            candles.append({
                **row(opened, close=100 + i),
                "close_time": utc_ms(opened + timedelta(minutes=15)),
                "source": "kraken",
                "received_at": utc_ms(opened + timedelta(minutes=15, seconds=2)),
                "available_at": utc_ms(opened + timedelta(minutes=15, seconds=2)),
                "is_final": True,
            })
        intrabar = {**row(start + timedelta(hours=5), close=999), "is_final": False}
        future_available = {
            **candles[-1], "open_time": utc_ms(start + timedelta(hours=5)),
            "close": 888, "available_at": utc_ms(start + timedelta(days=1)),
        }

        as_of = start + timedelta(hours=5, seconds=3)
        expected = data_fetcher.compute_indicators(candles, as_of=as_of)
        actual = data_fetcher.compute_indicators(
            candles + [intrabar, future_available], as_of=as_of
        )
        self.assertEqual(actual, expected)

    def test_kraken_fetch_uses_server_time_and_preserves_source(self):
        opened = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
        response = {
            "error": [],
            "result": {
                "XXBTZUSD": [
                    [int(opened.timestamp()), "100", "102", "99", "101", "0", "10", 3],
                ],
                "last": 0,
            },
        }

        def fake_get(url, retries=3):
            if url.endswith("/Time"):
                venue_time = int((opened + timedelta(minutes=10)).timestamp())
                return {"error": [], "result": {"unixtime": venue_time}}
            return response

        # Local receipt time is later, but venue time still says the bar is live.
        with patch.object(data_fetcher, "http_get", side_effect=fake_get), patch.object(
            data_fetcher, "_local_now", return_value=opened + timedelta(hours=1)
        ):
            batch = data_fetcher._kraken_ohlc("BTC", 900, 10)

        self.assertEqual(batch.closed, ())
        self.assertEqual(batch.intrabar[0].source, "kraken")


if __name__ == "__main__":
    unittest.main()
