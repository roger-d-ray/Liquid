import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import data_fetcher
from market_bars import (
    ClosedBar, normalize_ohlcv, only_available_closed_bars, utc_ms,
)


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


def hourly_row(opened, open_, high, low, close, volume):
    return {
        'open_time': utc_ms(opened),
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
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


class MarketFieldSemanticsTests(unittest.TestCase):
    def test_rolling_24h_uses_nearest_timestamped_price(self):
        current = datetime(2026, 7, 22, 12, 15, tzinfo=UTC)
        target = current - timedelta(hours=24)
        points = [
            {'timestamp': utc_ms(target - timedelta(minutes=20)), 'price': 100},
            {'timestamp': utc_ms(target + timedelta(minutes=10)), 'price': 110},
        ]
        change = data_fetcher.calculate_rolling_24h_change_pct(
            121, current, points
        )
        self.assertEqual(change, 10.0)
        reference = data_fetcher.select_rolling_24h_reference(current, points)
        self.assertEqual(
            reference['actual_interval_seconds'], 23 * 3600 + 50 * 60
        )

    def test_rolling_24h_rejects_reference_outside_tolerance(self):
        current = datetime(2026, 7, 22, 12, tzinfo=UTC)
        point = current - timedelta(hours=25)
        self.assertIsNone(data_fetcher.calculate_rolling_24h_change_pct(
            110, current, [{'timestamp': utc_ms(point), 'price': 100}]
        ))

    def test_coinbase_4h_numeric_aggregation(self):
        start = datetime(2026, 7, 22, 0, tzinfo=UTC)
        rows = [
            hourly_row(start, 100, 105, 99, 102, 1),
            hourly_row(start + timedelta(hours=1), 102, 110, 98, 108, 2),
            hourly_row(start + timedelta(hours=2), 108, 109, 97, 103, 3),
            hourly_row(start + timedelta(hours=3), 103, 106, 96, 104, 4),
        ]
        bar = data_fetcher._aggregate(rows, 4 * 3600)[0]
        self.assertEqual(
            (bar['open'], bar['high'], bar['low'], bar['close'], bar['volume']),
            (100, 110, 96, 104, 10),
        )
        self.assertEqual(bar['open_time'], utc_ms(start))
        self.assertEqual(bar['aggregation_method'], 'utc_aligned_4x1h')
        self.assertEqual(bar['completeness'], 'complete')
        self.assertEqual(bar['quality_flags'], ())

    def test_coinbase_4h_gap_is_incomplete_and_signal_ineligible(self):
        start = datetime(2026, 7, 22, 0, tzinfo=UTC)
        rows = [
            hourly_row(start, 100, 105, 99, 102, 1),
            hourly_row(start + timedelta(hours=1), 102, 110, 98, 108, 2),
            hourly_row(start + timedelta(hours=3), 103, 106, 96, 104, 4),
        ]
        aggregate = data_fetcher._aggregate(rows, 4 * 3600)[0]
        self.assertEqual(aggregate['completeness'], 'incomplete')
        self.assertIn('missing_component_bars', aggregate['quality_flags'])
        now = start + timedelta(hours=5)
        batch = normalize_ohlcv(
            [aggregate], 4 * 3600, 'coinbase',
            observed_at=now, received_at=now, grace_period_seconds=2,
        )
        self.assertEqual(batch.closed, ())
        self.assertEqual(len(batch.incomplete), 1)
        self.assertFalse(batch.incomplete[0].is_final)
        self.assertEqual(batch.incomplete[0].component_count, 3)
        self.assertEqual(
            batch.incomplete[0].missing_component_open_times,
            (utc_ms(start + timedelta(hours=2)),),
        )
        self.assertEqual(
            only_available_closed_bars(batch.incomplete, as_of=now), []
        )

    def test_change_since_midnight_switches_at_utc_day_boundary(self):
        day_one = datetime(2026, 7, 21, tzinfo=UTC)
        day_two = day_one + timedelta(days=1)
        daily = [
            {'open_time': utc_ms(day_one), 'open': 100},
            {'open_time': utc_ms(day_two), 'open': 200},
        ]
        before = day_two - timedelta(minutes=1)
        after = day_two + timedelta(minutes=1)
        self.assertEqual(
            data_fetcher.calculate_change_since_utc_midnight_pct(
                110, before, daily
            ),
            10.0,
        )
        self.assertEqual(
            data_fetcher.calculate_change_since_utc_midnight_pct(
                220, after, daily
            ),
            10.0,
        )

    def test_bar_extremes_are_renamed_and_day_levels_are_daily_only(self):
        start = datetime(2026, 5, 1, tzinfo=UTC)
        candles = []
        for i in range(60):
            opened = start + timedelta(days=i)
            candles.append(ClosedBar(
                open_time=utc_ms(opened),
                close_time=utc_ms(opened + timedelta(days=1)),
                open=100, high=200 + i, low=50 - i, close=100, volume=10,
                source='test', received_at=utc_ms(opened + timedelta(days=1)),
                available_at=utc_ms(opened + timedelta(days=1)),
            ).to_dict())
        as_of = start + timedelta(days=61)
        hourly = data_fetcher.compute_indicators(
            candles, as_of=as_of, timeframe='1h'
        )
        daily_indicators = data_fetcher.compute_indicators(
            candles, as_of=as_of, timeframe='1d'
        )
        self.assertEqual(hourly['high_20_bars'], 259)
        self.assertEqual(hourly['low_20_bars'], -9)
        self.assertEqual(hourly['high_55_bars'], 259)
        self.assertNotIn('high_20_days', hourly)
        self.assertEqual(daily_indicators['high_20_days'], 259)
        self.assertEqual(daily_indicators['low_20_days'], -9)
        self.assertNotIn('high_20', hourly)

    def test_kraken_volume_estimate_is_not_reported_as_exact_quote_volume(self):
        ticker = {'error': [], 'result': {'pair': {
            'c': ['110'], 'o': '100', 'v': ['1', '2'],
            'h': ['112', '115'], 'l': ['98', '95'],
        }}}
        observed = datetime(2026, 7, 22, 12, tzinfo=UTC)
        with patch.object(data_fetcher, 'http_get', return_value=ticker), patch.object(
            data_fetcher, '_venue_now', return_value=observed
        ):
            spot = data_fetcher._kraken_spot('BTC')
        self.assertEqual(spot['change_since_utc_midnight_pct'], 10)
        self.assertIsNone(spot['rolling_24h_change_pct'])
        self.assertEqual(spot['base_volume'], 2)
        self.assertIsNone(spot['quote_volume'])
        self.assertEqual(spot['estimated_quote_volume'], 220)


if __name__ == "__main__":
    unittest.main()
