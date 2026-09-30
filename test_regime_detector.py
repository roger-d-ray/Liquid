"""Test di regime_detector.py: integrita' fail-closed, silenzi, registro.

Ogni test lavora su una copia del bot in una directory temporanea, cosi' nessuna
manomissione tocca i file reali. La rete e' sempre finta: questi test non devono
dipendere da Coinbase.

    python3 -m unittest test_regime_detector -v
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import regime_detector as rd    # noqa: E402
import regime_source as rs      # noqa: E402

BOT = Path(__file__).parent
G = rs.GRANULARITY_SECONDS
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def make_bars(n, end_open):
    """n barre contigue e coerenti, che terminano a end_open (secondi)."""
    out = []
    for i in range(n - 1, -1, -1):
        t = end_open - i * G
        base = 100.0 + (t // G) % 41
        out.append({"open_time_ms": t * 1000, "open": base, "high": base + 2,
                    "low": base - 2, "close": base + 1, "volume": 10.0 + (t // G) % 7})
    return out


class BotCopy:
    """Copia minima del bot (moduli + models/) in una dir temporanea."""

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp())
        for m in rd.MODULES:
            shutil.copy2(BOT / m, self.dir / m)
        shutil.copytree(BOT / "models", self.dir / "models")
        return self.dir

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def rehash_meta(base: Path):
        """Riallinea i metadati dopo una modifica legittima dei file."""
        meta = json.loads((base / "models" / "regime_model.meta.json").read_text())
        for m in meta["modules"]:
            meta["modules"][m] = hashlib.sha256((base / m).read_bytes()).hexdigest()
        for info in meta["models"].values():
            info["sha256"] = hashlib.sha256((base / info["file"]).read_bytes()).hexdigest()
        (base / "models" / "regime_model.meta.json").write_text(json.dumps(meta, indent=2))


class IntegrityTest(unittest.TestCase):
    def test_intact_installation_passes(self):
        with BotCopy() as base:
            _, models, errors = rd.verify_integrity(base)
            self.assertEqual(errors, [])
            self.assertEqual(sorted(models), ["BTC", "ETH", "SOL"])

    def test_tampered_module_is_detected(self):
        with BotCopy() as base:
            (base / "regime_features.py").write_text(
                (base / "regime_features.py").read_text() + "\n# modifica\n")
            _, _, errors = rd.verify_integrity(base)
            self.assertTrue(any("regime_features.py" in e for e in errors), errors)

    def test_tampered_model_file_is_detected(self):
        with BotCopy() as base:
            p = base / "models" / "regime_BTC.json"
            m = json.loads(p.read_text())
            m["transmat"][0][0] = 0.5
            p.write_text(json.dumps(m))
            _, models, errors = rd.verify_integrity(base)
            self.assertTrue(any("alterato" in e for e in errors), errors)
            self.assertNotIn("BTC", models)

    def test_missing_model_file_is_detected(self):
        with BotCopy() as base:
            (base / "models" / "regime_ETH.json").unlink()
            _, models, errors = rd.verify_integrity(base)
            self.assertTrue(any("mancante" in e for e in errors), errors)
            self.assertNotIn("ETH", models)

    def test_missing_meta_is_detected(self):
        with BotCopy() as base:
            (base / "models" / "regime_model.meta.json").unlink()
            meta, models, errors = rd.verify_integrity(base)
            self.assertIsNone(meta)
            self.assertTrue(errors)

    def test_model_trained_on_another_source_is_detected(self):
        # Scenario reale: metadati che dichiarano una fonte diversa da quella del
        # modulo live. (Modificare il MODULO in una copia non e' simulabile qui —
        # i controlli semantici usano il modulo importato — ed e' gia' coperto
        # dall'hash: vedi la docstring di verify_integrity.)
        with BotCopy() as base:
            p = base / "models" / "regime_model.meta.json"
            meta = json.loads(p.read_text())
            meta["data_source"] = "kraken"
            p.write_text(json.dumps(meta, indent=2))
            _, _, errors = rd.verify_integrity(base)
            self.assertTrue(any("fonte" in e for e in errors), errors)

    def test_granularity_mismatch_is_detected(self):
        with BotCopy() as base:
            p = base / "models" / "regime_model.meta.json"
            meta = json.loads(p.read_text())
            meta["granularity_seconds"] = 900
            p.write_text(json.dumps(meta, indent=2))
            _, _, errors = rd.verify_integrity(base)
            self.assertTrue(any("granularita" in e for e in errors), errors)

    def test_hand_edited_labels_are_detected(self):
        with BotCopy() as base:
            p = base / "models" / "regime_SOL.json"
            m = json.loads(p.read_text())
            m["labels"] = {k: ("trend" if v == "range" else "range")
                           for k, v in m["labels"].items()}
            p.write_text(json.dumps(m))
            BotCopy.rehash_meta(base)
            _, models, errors = rd.verify_integrity(base)
            self.assertTrue(any("etichette" in e for e in errors), errors)
            self.assertNotIn("SOL", models)

    def test_integrity_failure_makes_no_network_call(self):
        called = []
        original = rs.fetch_recent_bars
        rs.fetch_recent_bars = lambda *a, **k: called.append(1)
        try:
            with BotCopy() as base:
                (base / "regime_hmm.py").write_text(
                    (base / "regime_hmm.py").read_text() + "\n# modifica\n")
                out = rd.run(use_register=False, base=base)
        finally:
            rs.fetch_recent_bars = original
        self.assertFalse(out["integrity_ok"])
        self.assertEqual(called, [], "non deve toccare la rete senza integrita'")
        self.assertEqual(sorted(out["assets"]), ["BTC", "ETH", "SOL"])
        for res in out["assets"].values():
            self.assertIsNone(res["tradable_regime"])
            self.assertEqual(res["reason"], "integrity_failed")
            self.assertIn("regime_hmm.py", res["detail"])


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.meta = json.loads((BOT / "models" / "regime_model.meta.json").read_text())
        self.model = json.loads((BOT / "models" / "regime_BTC.json").read_text())
        self.fail_safe = self.meta["fail_safe"]

    def _with_fetch(self, fn, now=None):
        original = rs.fetch_recent_bars
        rs.fetch_recent_bars = fn
        try:
            return rd.detect_asset("BTC", self.model, self.fail_safe, now=now)
        finally:
            rs.fetch_recent_bars = original

    def _with_fetch_at(self, fn, now):
        return self._with_fetch(fn, now=now)

    def test_healthy_fetch_yields_labelled_regime(self):
        end = rs.last_closed_open_time(NOW.timestamp())
        bars = make_bars(self.fail_safe["target_contiguous_bars"], end)
        res = self._with_fetch(lambda *a, **k: bars)
        self.assertIn(res["state"], ("range", "trend"))
        self.assertTrue(0.0 <= res["confidence"] <= 1.0)
        self.assertEqual(res["feature_rows"],
                         len(bars) - rd.rf.FEATURE_WINDOW_BARS + 1)

    def test_confidence_comes_from_forward_filtering_not_viterbi(self):
        end = rs.last_closed_open_time(NOW.timestamp())
        bars = make_bars(self.fail_safe["target_contiguous_bars"], end)
        res = self._with_fetch(lambda *a, **k: bars)
        rows = rd.feature_rows(bars)
        expected = rd.regime_hmm.filtered_posteriors(self.model, rows)[-1]
        self.assertAlmostEqual(res["confidence"], max(expected), places=12)

    def test_each_source_failure_maps_to_its_own_code(self):
        for exc, code in ((rs.SourceUnavailable("giu'"), "source_unavailable"),
                          (rs.StaleData("vecchio"), "stale_data"),
                          (rs.StitchError("giunzione"), "stitch_error")):
            def boom(*a, _e=exc, **k):
                raise _e
            res = self._with_fetch(boom)
            self.assertIsNone(res["state"])
            self.assertIsNone(res["tradable_regime"])
            self.assertEqual(res["reason"], code)

    def test_insufficient_history_reports_recovery_estimate(self):
        def boom(*a, **k):
            raise rs.InsufficientHistory("120 barre contigue, minimo 249")
        end = rs.last_closed_open_time(NOW.timestamp())
        target = self.fail_safe["target_contiguous_bars"]
        available = {t: {} for t in range(end - 119 * G, end + 1, G)}
        original = rs.fetch_range
        rs.fetch_range = lambda *a, **k: (available, [])
        try:
            res = self._with_fetch_at(boom, NOW.timestamp())
        finally:
            rs.fetch_range = original
        self.assertEqual(res["reason"], "insufficient_history")
        self.assertEqual(res["contiguous_bars"], 120)
        self.assertEqual(res["bars_missing"], self.fail_safe["min_contiguous_bars"] - 120)
        self.assertIn("expected_recovery", res)
        self.assertLess(target, 10_000)

    def test_recovery_estimate_failure_does_not_break_refusal(self):
        def boom(*a, **k):
            raise rs.InsufficientHistory("troppo corta")
        original = rs.fetch_range
        rs.fetch_range = lambda *a, **k: (_ for _ in ()).throw(rs.SourceUnavailable("giu'"))
        try:
            res = self._with_fetch(boom)
        finally:
            rs.fetch_range = original
        self.assertEqual(res["reason"], "insufficient_history")
        self.assertNotIn("expected_recovery", res)

    def test_state_without_label_is_refused(self):
        # Guardia locale di detect_asset: in produzione verify_integrity la rende
        # irraggiungibile, ma detect_asset e' chiamabile da sola e non deve mai
        # restituire uno stato senza nome. (Lacuna trovata col mutation testing.)
        end = rs.last_closed_open_time(NOW.timestamp())
        bars = make_bars(self.fail_safe["target_contiguous_bars"], end)
        broken = dict(self.model, labels={})
        original = rs.fetch_recent_bars
        rs.fetch_recent_bars = lambda *a, **k: bars
        try:
            res = rd.detect_asset("BTC", broken, self.fail_safe, now=NOW.timestamp())
        finally:
            rs.fetch_recent_bars = original
        self.assertIsNone(res["state"])
        self.assertEqual(res["reason"], "unlabelled_state")

    def test_too_few_feature_rows_is_refused(self):
        end = rs.last_closed_open_time(NOW.timestamp())
        bars = make_bars(rd.rf.FEATURE_WINDOW_BARS + 5, end)   # 6 righe, minimo 50
        res = self._with_fetch(lambda *a, **k: bars)
        self.assertEqual(res["reason"], "insufficient_feature_rows")


class VerdictTest(unittest.TestCase):
    """La soglia di confidence la applica il detector: chi legge riceve un verdetto."""

    def setUp(self):
        self.model = json.loads((BOT / "models" / "regime_BTC.json").read_text())
        self.fs = json.loads((BOT / "models" / "regime_model.meta.json").read_text())["fail_safe"]
        end = rs.last_closed_open_time(NOW.timestamp())
        self.bars = make_bars(self.fs["target_contiguous_bars"], end)

    def _detect_with_confidence(self, conf, label="range"):
        orig_fetch, orig_pred = rs.fetch_recent_bars, rd.regime_hmm.predict_regime
        rs.fetch_recent_bars = lambda *a, **k: self.bars
        rd.regime_hmm.predict_regime = lambda m, rows: {
            "state": 0, "label": label, "confidence": conf, "probs": [conf, 1 - conf]}
        try:
            return rd.detect_asset("BTC", self.model, self.fs, now=NOW.timestamp())
        finally:
            rs.fetch_recent_bars, rd.regime_hmm.predict_regime = orig_fetch, orig_pred

    def test_above_threshold_is_tradable(self):
        res = self._detect_with_confidence(0.97, "trend")
        self.assertEqual(res["tradable_regime"], "trend")
        self.assertIsNone(res["reason"])

    def test_exactly_at_threshold_is_tradable(self):
        res = self._detect_with_confidence(self.fs["min_confidence"])
        self.assertEqual(res["tradable_regime"], "range")

    def test_below_threshold_keeps_state_but_is_not_tradable(self):
        res = self._detect_with_confidence(0.9499)
        self.assertIsNone(res["tradable_regime"])
        self.assertEqual(res["state"], "range")                 # diagnostico
        self.assertEqual(res["reason"], "below_confidence_threshold")

    def test_missing_threshold_is_an_integrity_error(self):
        for bad in (None, 0.4, 1.2, "0.95", True):
            with self.subTest(min_confidence=bad), BotCopy() as base:
                p = base / "models" / "regime_model.meta.json"
                meta = json.loads(p.read_text())
                if bad is None:
                    meta["fail_safe"].pop("min_confidence")
                else:
                    meta["fail_safe"]["min_confidence"] = bad
                p.write_text(json.dumps(meta))
                _, _, errors = rd.verify_integrity(base)
                self.assertTrue(any("min_confidence" in e for e in errors), errors)

    def test_low_confidence_is_not_a_silence(self):
        reg = rd.SilenceRegister(Path(tempfile.mkdtemp()) / "r.jsonl")
        low = {"tradable_regime": None, "state": "trend", "confidence": 0.80,
               "reason": "below_confidence_threshold", "detail": "sotto soglia"}
        self.assertEqual(rd.update_register(reg, "BTC", low, NOW), [])
        self.assertFalse(reg.is_silent("BTC"))
        self.assertEqual(reg.pending, [])

    def test_resumption_below_threshold_says_so(self):
        reg = rd.SilenceRegister(Path(tempfile.mkdtemp()) / "r.jsonl")
        rd.update_register(reg, "BTC", {"state": None, "reason": "stale_data",
                                        "detail": "vecchio"}, NOW)
        notes = rd.update_register(reg, "BTC", {"tradable_regime": None, "state": "range",
                                                "confidence": 0.9, "reason":
                                                "below_confidence_threshold"},
                                   NOW + timedelta(hours=2))
        self.assertIn("sotto soglia", notes[0])


class RobustnessTest(unittest.TestCase):
    """Nessuna eccezione esce da run(); il tempo e' limitato."""

    def test_expired_deadline_makes_no_network_call(self):
        called = []
        out = rd.run(use_register=False, deadline_seconds=0.0,
                     http_get=lambda url: called.append(url))
        self.assertEqual(called, [])
        self.assertTrue(all(r["reason"] == "detector_deadline" for r in out["assets"].values()))

    def test_deadline_http_get_refuses_after_deadline(self):
        import time as _t
        with self.assertRaises(rd.DeadlineExceeded):
            rd.deadline_http_get(_t.monotonic() - 1)("https://example.invalid")

    def test_deadline_during_fetch_maps_to_detector_deadline(self):
        def expired(url):
            raise rd.DeadlineExceeded("finito")
        model = json.loads((BOT / "models" / "regime_BTC.json").read_text())
        fs = json.loads((BOT / "models" / "regime_model.meta.json").read_text())["fail_safe"]
        res = rd.detect_asset("BTC", model, fs, now=NOW.timestamp(),
                              http_get=expired, sleep=lambda s: None)
        self.assertEqual(res["reason"], "detector_deadline")

    def test_unexpected_error_is_isolated_per_asset(self):
        end = rs.last_closed_open_time(NOW.timestamp())
        good = make_bars(300, end)
        def fetch(asset, **k):
            if asset == "ETH":
                raise ZeroDivisionError("bug imprevisto")
            return good
        orig = rs.fetch_recent_bars
        rs.fetch_recent_bars = fetch
        try:
            out = rd.run(use_register=False)
        finally:
            rs.fetch_recent_bars = orig
        self.assertEqual(out["assets"]["ETH"]["reason"], "detector_error")
        self.assertIn("ZeroDivisionError", out["assets"]["ETH"]["detail"])
        self.assertIsNotNone(out["assets"]["BTC"]["state"])     # gli altri proseguono
        self.assertIsNotNone(out["assets"]["SOL"]["state"])

    def test_register_write_failure_does_not_crash(self):
        with BotCopy() as base:
            (base / "logs").mkdir()
            (base / "logs" / "regime_silence.jsonl").mkdir()     # scrittura impossibile
            orig = rs.fetch_recent_bars
            rs.fetch_recent_bars = lambda *a, **k: (_ for _ in ()).throw(
                rs.SourceUnavailable("giu'"))
            try:
                out = rd.run(base=base)
            finally:
                rs.fetch_recent_bars = orig
        self.assertIn("register_error", out)

    def test_integrity_failure_notifies_once_through_register(self):
        with BotCopy() as base:
            (base / "regime_hmm.py").write_text(
                (base / "regime_hmm.py").read_text() + "\n# modifica\n")
            first = rd.run(base=base)
            second = rd.run(base=base)
        self.assertEqual(len(first["notifications"]), 3)          # uno per asset
        self.assertTrue(all(n.startswith("🛑") for n in first["notifications"]))
        self.assertEqual(second["notifications"], [])             # niente a ogni run


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.reg = rd.SilenceRegister(self.dir / "regime_silence.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_notifies_once_at_start_and_once_at_recovery(self):
        silent = {"state": None, "reason": "source_unavailable", "detail": "giu'"}
        live = {"state": "range", "confidence": 0.97}
        first = rd.update_register(self.reg, "BTC", silent, NOW)
        self.assertEqual(len(first), 1)
        self.assertIn("🔇", first[0])
        for i in range(1, 6):                       # 5 ore di silenzio continuato
            self.assertEqual(rd.update_register(self.reg, "BTC", silent,
                                                NOW + timedelta(hours=i)), [])
        back = rd.update_register(self.reg, "BTC", live, NOW + timedelta(hours=6))
        self.assertEqual(len(back), 1)
        self.assertIn("🔊", back[0])
        self.assertEqual(rd.update_register(self.reg, "BTC", live,
                                            NOW + timedelta(hours=7)), [])

    def test_notification_carries_cause_and_recovery(self):
        silent = {"state": None, "reason": "insufficient_history", "detail": "120/249",
                  "expected_recovery": "2026-10-05T12:00:00+00:00",
                  "bars_missing": 129, "gap_ends_at": "2026-09-25T03:00:00+00:00"}
        msg = rd.update_register(self.reg, "BTC", silent, NOW)[0]
        for fragment in ("insufficient_history", "2026-10-05", "2026-09-25", "129"):
            self.assertIn(fragment, msg)
        self.assertIn("Gestione posizioni aperte invariata", msg)

    def test_assets_are_tracked_independently(self):
        silent = {"state": None, "reason": "source_unavailable", "detail": "giu'"}
        live = {"state": "trend", "confidence": 0.9}
        rd.update_register(self.reg, "BTC", silent, NOW)
        self.assertEqual(rd.update_register(self.reg, "ETH", live, NOW), [])
        self.assertTrue(self.reg.is_silent("BTC"))
        self.assertFalse(self.reg.is_silent("ETH"))

    def test_silence_fraction_counts_open_period_and_clips_window(self):
        self.reg.append({"ts": rd._iso(NOW - timedelta(days=100)), "asset": "BTC",
                         "event": "silence_start"})
        self.reg.append({"ts": rd._iso(NOW - timedelta(days=80)), "asset": "BTC",
                         "event": "silence_end"})
        # 100->80 giorni fa: dentro la finestra di 90 solo i 10 giorni piu' recenti
        self.assertAlmostEqual(self.reg.silence_fraction("BTC", NOW), 10 / 90 * 100, places=6)
        self.reg.append({"ts": rd._iso(NOW - timedelta(days=9)), "asset": "BTC",
                         "event": "silence_start"})        # ancora aperto
        self.assertAlmostEqual(self.reg.silence_fraction("BTC", NOW),
                               19 / 90 * 100, places=6)

    def test_budget_alert_fires_once_then_recovers(self):
        self.reg.append({"ts": rd._iso(NOW - timedelta(days=10)), "asset": "BTC",
                         "event": "silence_start"})
        self.reg.append({"ts": rd._iso(NOW - timedelta(days=4)), "asset": "BTC",
                         "event": "silence_end"})          # 6/90 = 6,7% > 5%
        live = {"state": "range", "confidence": 0.95}
        notes = rd.update_register(self.reg, "BTC", live, NOW)
        self.assertTrue(any("Budget di silenzio superato" in n for n in notes))
        self.assertEqual(rd.update_register(self.reg, "BTC", live,
                                            NOW + timedelta(hours=1)), [])
        # 100 giorni dopo il silenzio esce dalla finestra: rientro, una volta sola
        later = NOW + timedelta(days=100)
        notes = rd.update_register(self.reg, "BTC", live, later)
        self.assertTrue(any("rientrato" in n for n in notes))
        self.assertEqual(rd.update_register(self.reg, "BTC", live,
                                            later + timedelta(hours=1)), [])

    def test_survives_corrupt_lines(self):
        path = self.dir / "r.jsonl"
        path.write_text('{"ts": "%s", "asset": "BTC", "event": "silence_start"}\n'
                        'non-json\n\n{"senza": "campi"}\n' % rd._iso(NOW))
        reg = rd.SilenceRegister(path)
        self.assertTrue(reg.is_silent("BTC"))

    def test_state_does_not_depend_on_line_order(self):
        # Il fallback API di git_push_log puo' accodare righe fuori ordine:
        # lo stato deve dipendere dai timestamp, non dalla posizione nel file.
        start = {"ts": rd._iso(NOW - timedelta(hours=5)), "asset": "BTC",
                 "event": "silence_start"}
        end = {"ts": rd._iso(NOW - timedelta(hours=1)), "asset": "BTC",
               "event": "silence_end"}
        path = self.dir / "fuori_ordine.jsonl"
        path.write_text(json.dumps(end) + "\n" + json.dumps(start) + "\n")
        reg = rd.SilenceRegister(path)
        self.assertFalse(reg.is_silent("BTC"))
        self.assertAlmostEqual(reg.silence_fraction("BTC", NOW), 4 / (90 * 24) * 100, places=6)

    def test_unparseable_timestamp_is_skipped(self):
        path = self.dir / "ts_rotto.jsonl"
        path.write_text('{"ts": "ieri", "asset": "BTC", "event": "silence_start"}\n')
        self.assertFalse(rd.SilenceRegister(path).is_silent("BTC"))

    def test_events_are_appended_not_rewritten(self):
        path = self.dir / "r.jsonl"
        reg = rd.SilenceRegister(path)
        reg.append({"ts": rd._iso(NOW), "asset": "BTC", "event": "silence_start"})
        reg.flush()
        reg2 = rd.SilenceRegister(path)
        reg2.append({"ts": rd._iso(NOW + timedelta(hours=1)), "asset": "BTC",
                     "event": "silence_end"})
        reg2.flush()
        self.assertEqual(len(path.read_text().strip().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
