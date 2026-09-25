"""Test di regime_source.py: paginazione, cucitura, retry, fail-closed.

Il Coinbase finto riproduce la semantica MISURATA dell'endpoint reale: [start,
end] inclusivi, righe [time, low, high, open, close, volume] dalla piu' recente.
Ogni candela ha valori che dipendono dal suo timestamp, cosi' uno scambio o una
duplicazione di barre produrrebbe valori sbagliati e verrebbe notato.

Esecuzione:  python3 -m unittest discover -s regime-training -p 'test_regime_source.py'
Test live:   REGIME_LIVE_TESTS=1 python3 -m unittest ...   (rete verso Coinbase)
"""

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import regime_source as rs  # noqa: E402

G = rs.GRANULARITY_SECONDS
NOW = 1_790_000_000 + 30 * 60          # istante fisso a meta' ora
END = rs.last_closed_open_time(NOW)    # ultima barra chiusa attesa


def price(t):
    return 100.0 + (t // G) % 97


class FakeCoinbase:
    """http_get finto. `available`: open_time presenti alla fonte. `failures`:
    per indice di chiamata, eccezione da sollevare. `mutate`: funzione che puo'
    alterare le righe di una risposta (per iniettare difetti)."""

    def __init__(self, available, failures=None, mutate=None):
        self.available = set(available)
        self.failures = dict(failures or {})
        self.mutate = mutate
        self.calls = []

    def __call__(self, url):
        q = parse_qs(urlparse(url).query)
        a = int(datetime.fromisoformat(q["start"][0]).timestamp())
        b = int(datetime.fromisoformat(q["end"][0]).timestamp())
        idx = len(self.calls)
        self.calls.append((a, b))
        if idx in self.failures:
            raise self.failures[idx]
        rows = [[t, price(t) - 1, price(t) + 1, price(t), price(t) + 0.5, 10.0]
                for t in range(b, a - 1, -G) if t in self.available]
        return self.mutate(idx, rows) if self.mutate else rows


def full_history(n):
    return {END - i * G for i in range(n)}


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, s):
        self.calls.append(s)


class PlanPagesTest(unittest.TestCase):
    def test_pages_are_disjoint_adjacent_and_exact(self):
        for n in (1, 299, 300, 301, 600, 901, 17520):
            pages = rs.plan_pages(END, n)
            covered = []
            for a, b in pages:
                self.assertLessEqual((b - a) // G + 1, rs.PAGE_BARS)
                covered.extend(range(a, b + 1, G))
            self.assertEqual(len(covered), n, f"n={n}")
            self.assertEqual(len(set(covered)), n, f"sovrapposizioni con n={n}")
            self.assertEqual(sorted(covered), list(range(END - (n - 1) * G, END + 1, G)))
            for (a_new, _), (_, b_old) in zip(pages, pages[1:]):
                self.assertEqual(b_old + G, a_new, "pagine non adiacenti")


class StitchingTest(unittest.TestCase):
    def test_multi_page_stitch_exact_timestamps(self):
        fake = FakeCoinbase(full_history(800))
        bars = rs.fetch_recent_bars("BTC", min_bars=249, target_bars=750, now=NOW,
                                    http_get=fake, sleep=Sleeps())
        ts = [b["open_time_ms"] // 1000 for b in bars]
        self.assertEqual(len(fake.calls), 3)                      # 300 + 300 + 150
        self.assertEqual(ts, list(range(END - 749 * G, END + 1, G)))   # esatto
        for b in bars:                                            # valori al posto giusto
            self.assertEqual(b["open"], price(b["open_time_ms"] // 1000))
        spans = sorted(fake.calls)                                # richieste disgiunte
        for (a1, b1), (a2, _) in zip(spans, spans[1:]):
            self.assertEqual(b1 + G, a2)

    def test_bar_outside_requested_page_is_rejected(self):
        # Pagina SINGOLA: nessuna pagina successiva con cui la barra estranea
        # potrebbe collidere, quindi l'unico controllo che puo' fermarla e'
        # quello di range. (La versione multi-pagina passava per il motivo
        # sbagliato: la fermava il controllo sovrapposizioni. Scoperto con un
        # test per mutazione.)
        def mutate(idx, rows):
            t = END + G                                           # barra "dal futuro"
            return [[t, price(t) - 1, price(t) + 1, price(t), price(t), 1.0]] + rows
        fake = FakeCoinbase(full_history(400), mutate=mutate)
        with self.assertRaises(rs.StitchError):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                 http_get=fake, sleep=Sleeps())

    def test_duplicate_timestamp_is_rejected(self):
        fake = FakeCoinbase(full_history(800), mutate=lambda i, r: r + [r[5]])
        with self.assertRaises(rs.StitchError):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                 http_get=fake, sleep=Sleeps())

    def test_gap_exactly_at_seam_is_rejected(self):
        seam_old = END - 300 * G                                  # b della 2a pagina
        fake = FakeCoinbase(full_history(800) - {seam_old})
        with self.assertRaises(rs.StitchError):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=600, now=NOW,
                                 http_get=fake, sleep=Sleeps())


class FailureTest(unittest.TestCase):
    def test_intermediate_page_transient_failure_is_retried(self):
        sleeps = Sleeps()
        fake = FakeCoinbase(full_history(800),
                            failures={1: rs.HttpStatusError(503, "busy")})
        bars = rs.fetch_recent_bars("BTC", min_bars=249, target_bars=750, now=NOW,
                                    http_get=fake, sleep=sleeps)
        self.assertEqual(len(bars), 750)
        self.assertEqual(len(fake.calls), 4)                      # 3 pagine + 1 retry
        self.assertIn(1, sleeps.calls)                            # backoff 2**0

    def test_intermediate_page_persistent_failure_never_returns_partial(self):
        failures = {i: rs.HttpStatusError(503, "busy") for i in range(1, 10)}
        fake = FakeCoinbase(full_history(800), failures=failures)
        with self.assertRaises(rs.SourceUnavailable):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=750, now=NOW,
                                 http_get=fake, sleep=Sleeps())
        # 1a pagina ok, poi 1 + MAX_RETRIES tentativi sulla 2a, e basta
        self.assertEqual(len(fake.calls), 1 + 1 + rs.MAX_RETRIES)

    def test_rate_limit_429_retried_then_refused(self):
        sleeps = Sleeps()
        failures = {i: rs.HttpStatusError(429, "Too Many Requests") for i in range(10)}
        fake = FakeCoinbase(full_history(400), failures=failures)
        with self.assertRaises(rs.SourceUnavailable):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                 http_get=fake, sleep=sleeps)
        self.assertEqual(sleeps.calls, [1, 2, 4])                 # backoff esponenziale

    def test_non_transient_error_fails_fast(self):
        fake = FakeCoinbase(full_history(400),
                            failures={0: rs.HttpStatusError(403, "Forbidden")})
        with self.assertRaises(rs.SourceUnavailable):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                 http_get=fake, sleep=Sleeps())
        self.assertEqual(len(fake.calls), 1)                      # nessun retry inutile

    def test_network_error_is_transient(self):
        fake = FakeCoinbase(full_history(400), failures={0: TimeoutError("timeout")})
        bars = rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                    http_get=fake, sleep=Sleeps())
        self.assertEqual(len(bars), 300)


class ContiguityAndFreshnessTest(unittest.TestCase):
    def test_gap_inside_required_window_is_refused(self):
        fake = FakeCoinbase(full_history(400) - {END - 100 * G})
        with self.assertRaises(rs.InsufficientHistory):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                 http_get=fake, sleep=Sleeps())

    def test_gap_older_than_required_window_uses_contiguous_tail(self):
        fake = FakeCoinbase(full_history(400) - {END - 270 * G})
        bars = rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                    http_get=fake, sleep=Sleeps())
        ts = [b["open_time_ms"] // 1000 for b in bars]
        self.assertEqual(len(ts), 270)
        self.assertTrue(all(y - x == G for x, y in zip(ts, ts[1:])))
        self.assertEqual(ts[-1], END)

    def test_missing_last_closed_bar_is_stale(self):
        fake = FakeCoinbase(full_history(400) - {END})
        with self.assertRaises(rs.StaleData):
            rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                 http_get=fake, sleep=Sleeps())

    def test_forming_bar_is_never_used(self):
        forming = END + G
        fake = FakeCoinbase(full_history(400) | {forming})
        bars = rs.fetch_recent_bars("BTC", min_bars=249, target_bars=300, now=NOW,
                                    http_get=fake, sleep=Sleeps())
        self.assertEqual(bars[-1]["open_time_ms"] // 1000, END)
        self.assertTrue(all(b <= END for _, b in fake.calls))     # mai richiesta

    def test_close_grace(self):
        top = 1_790_000_000 // G * G
        self.assertEqual(rs.last_closed_open_time(top + 30), top - 2 * G)  # grace non trascorso
        self.assertEqual(rs.last_closed_open_time(top + 61), top - G)


class ParserTest(unittest.TestCase):
    def test_valid_row(self):
        c = rs.parse_candle_row([7200, 9.0, 11.0, 10.0, 10.5, 3.0])
        self.assertEqual(c, {"open_time_ms": 7_200_000, "open": 10.0, "high": 11.0,
                             "low": 9.0, "close": 10.5, "volume": 3.0})

    def test_invalid_rows(self):
        for row in ([7200, 11, 9, 10, 10, 1],        # low > high
                    [7200, 9, 11, 10, 10, -1],       # volume negativo
                    [7201, 9, 11, 10, 10, 1],        # non allineato
                    [7200, 9, 11, 10, float("nan"), 1],
                    [7200, 9, 11, 10],               # troppo corta
                    "garbage"):
            with self.assertRaises(rs.InvalidCandle, msg=repr(row)):
                rs.parse_candle_row(row)


@unittest.skipUnless(os.environ.get("REGIME_LIVE_TESTS") == "1",
                     "test live disattivato (REGIME_LIVE_TESTS=1 per abilitarlo)")
class LiveCoinbaseTest(unittest.TestCase):
    def test_three_page_stitch_matches_independent_request_across_seams(self):
        bars = rs.fetch_recent_bars("BTC", min_bars=249, target_bars=750)
        stitched = {b["open_time_ms"] // 1000: b for b in bars}
        ts = sorted(stitched)
        self.assertTrue(all(y - x == G for x, y in zip(ts, ts[1:])), "buco")
        end = ts[-1]
        # Per ogni giunzione: richiesta indipendente che la ATTRAVERSA, e
        # confronto barra per barra con la serie ricucita.
        for seam in (end - 299 * G, end - 599 * G):
            if seam - 5 * G < ts[0]:
                continue
            independent = rs.fetch_page("BTC-USD", seam - 5 * G, seam + 5 * G)
            self.assertEqual(len(independent), 11)
            for t, bar in independent.items():
                self.assertEqual(bar, stitched[t], f"giunzione {t}")


if __name__ == "__main__":
    unittest.main()
