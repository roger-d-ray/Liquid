"""Test di regime_gate.py: il regime non deve mai rompere la routine.

Ogni modo di fallire del detector viene simulato con uno script finto eseguito
davvero in un sottoprocesso (timeout, crash, output non JSON, verdetti
incoerenti, exit code inattesi). Nessuna rete: il detector reale non viene
lanciato. I log vanno in una directory temporanea, mai nei logs/ del repo.

    python3 -m unittest test_regime_gate -v
"""

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import regime_gate as rg  # noqa: E402


def ok_entry(state="range", conf=0.99, thr=0.95):
    if conf >= thr:
        return {"tradable_regime": state, "state": state, "confidence": conf,
                "reason": None, "detail": None, "as_of": "2026-09-30T07:00:00+00:00",
                "min_confidence": thr}
    return {"tradable_regime": None, "state": state, "confidence": conf,
            "reason": "below_confidence_threshold", "detail": "sotto soglia",
            "as_of": "2026-09-30T07:00:00+00:00", "min_confidence": thr}


def payload(assets, **extra):
    return {"schema_version": 1, "assets": assets, "notifications": [],
            "integrity_ok": True, "register_events_loaded": 0, **extra}


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.set_switch("ON\n")          # stato normale; i test dell'interruttore lo cambiano

    def set_switch(self, text):
        path = self.tmp / rg.SWITCH_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")

    def script(self, body: str) -> Path:
        p = self.tmp / f"fake_{len(list(self.tmp.glob('fake_*')))}.py"
        p.write_text("import json, sys, time\n" + textwrap.dedent(body))
        return p

    def emit(self, data, exit_code=0) -> Path:
        # json.dumps due volte: il JSON diventa un letterale stringa valido in Python
        return self.script(f"print({json.dumps(json.dumps(data))})\n"
                           f"sys.exit({exit_code})\n")

    def gate(self, script, **kw):
        kw.setdefault("timeout", 5)
        return rg.get_market_regime(base=self.tmp, script=script, **kw)

    def assert_all_null(self, res, reason):
        self.assertEqual(sorted(res["per_asset"]), ["BTC", "ETH", "SOL"])
        for v in res["per_asset"].values():
            self.assertIsNone(v["tradable_regime"])
            self.assertEqual(v["reason"], reason)


class HappyPathTest(Gate):
    def test_valid_verdicts_pass_through(self):
        res = self.gate(self.emit(payload({"BTC": ok_entry("range"), "ETH": ok_entry("trend"),
                                           "SOL": ok_entry("range", 0.80)})))
        self.assertEqual(res["per_asset"]["BTC"]["tradable_regime"], "range")
        self.assertEqual(res["per_asset"]["ETH"]["tradable_regime"], "trend")
        sol = res["per_asset"]["SOL"]
        self.assertIsNone(sol["tradable_regime"])
        self.assertEqual((sol["state"], sol["reason"]), ("range", "below_confidence_threshold"))
        self.assertEqual(res["telemetry"]["outcome"], "ok")

    def test_integrity_failure_output_is_accepted_as_valid(self):
        nul = {"tradable_regime": None, "state": None, "confidence": None,
               "reason": "integrity_failed", "detail": "hash"}
        res = self.gate(self.emit(payload({a: nul for a in rg.ASSETS}, integrity_ok=False,
                                          integrity_errors=["hash"]), exit_code=1))
        self.assert_all_null(res, "integrity_failed")
        self.assertEqual(res["telemetry"]["outcome"], "ok")
        self.assertEqual(res["telemetry"]["integrity_errors"], ["hash"])


class ProcessFailureTest(Gate):
    def test_timeout(self):
        res = self.gate(self.script("time.sleep(30)\n"), timeout=1)
        self.assert_all_null(res, "detector_timeout")

    def test_crash(self):
        res = self.gate(self.script("raise RuntimeError('boom')\n"))
        self.assert_all_null(res, "detector_crashed")
        self.assertIn("boom", res["per_asset"]["BTC"]["detail"])

    def test_garbage_output(self):
        res = self.gate(self.script("print('non sono json')\n"))
        self.assert_all_null(res, "detector_invalid_output")

    def test_unexpected_exit_code_even_with_json(self):
        res = self.gate(self.emit(payload({a: ok_entry() for a in rg.ASSETS}), exit_code=3))
        self.assert_all_null(res, "detector_crashed")

    def test_wrong_top_level_structure(self):
        res = self.gate(self.emit({"schema_version": 1, "assets": "no"}))
        self.assert_all_null(res, "detector_invalid_output")

    def test_launch_failure(self):
        res = self.gate(self.tmp / "non_esiste.py", python="/percorso/inesistente/python")
        self.assert_all_null(res, "detector_launch_failed")

    def test_gate_never_raises(self):
        with mock.patch.object(rg, "run_detector", side_effect=MemoryError("finta")):
            res = rg.get_market_regime(base=self.tmp)
        self.assert_all_null(res, "gate_error")
        self.assertEqual(len(res["notifications"]), 1)


class ValidationTest(Gate):
    """Il gate non si fida ciecamente: un verdetto operativo deve essere coerente."""

    def check(self, entry):
        assets = {a: ok_entry() for a in rg.ASSETS}
        assets["BTC"] = entry
        return self.gate(self.emit(payload(assets)))["per_asset"]["BTC"]

    def test_tradable_below_its_threshold_is_rejected(self):
        e = ok_entry(); e["confidence"] = 0.50
        self.assertEqual(self.check(e)["reason"], "detector_output_invalid")

    def test_tradable_differs_from_state_is_rejected(self):
        e = ok_entry("range"); e["state"] = "trend"
        self.assertEqual(self.check(e)["reason"], "detector_output_invalid")

    def test_unknown_state_is_rejected(self):
        e = ok_entry(); e["tradable_regime"] = e["state"] = "transition"
        self.assertEqual(self.check(e)["reason"], "detector_output_invalid")

    def test_null_without_reason_is_rejected(self):
        e = ok_entry(conf=0.5); e["reason"] = None
        self.assertEqual(self.check(e)["reason"], "detector_output_invalid")

    def test_missing_asset_is_null(self):
        res = self.gate(self.emit(payload({"BTC": ok_entry(), "ETH": ok_entry()})))
        self.assertEqual(res["per_asset"]["SOL"]["reason"], "detector_output_invalid")
        self.assertEqual(res["per_asset"]["BTC"]["tradable_regime"], "range")


class NotificationAndTelemetryTest(Gate):
    def test_process_failure_notifies_once_then_recovers(self):
        broken = self.script("raise RuntimeError('boom')\n")
        first = self.gate(broken)
        second = self.gate(broken)
        self.assertEqual(len(first["notifications"]), 3)          # una per asset
        self.assertEqual(second["notifications"], [])             # non a ogni run
        silence = (self.tmp / "logs" / "regime_silence.jsonl").read_text().splitlines()
        self.assertEqual(len(silence), 3)

    def test_telemetry_chains_runs_through_the_log(self):
        good = self.emit(payload({a: ok_entry() for a in rg.ASSETS}))
        first = self.gate(good)
        second = self.gate(good)
        lines = [json.loads(l) for l in
                 (self.tmp / "logs" / "regime_runs.jsonl").read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertIsNone(lines[0]["prev_run_ts"])
        self.assertEqual(lines[1]["prev_run_ts"], lines[0]["ts"])  # prova di persistenza
        self.assertEqual(lines[1]["assets"], {a: "range" for a in rg.ASSETS})
        self.assertIn("elapsed_s", second["telemetry"])

    def test_telemetry_write_failure_does_not_break(self):
        (self.tmp / "logs").mkdir()
        (self.tmp / "logs" / "regime_runs.jsonl").mkdir()          # scrittura impossibile
        res = self.gate(self.emit(payload({a: ok_entry() for a in rg.ASSETS})))
        self.assertEqual(res["per_asset"]["BTC"]["tradable_regime"], "range")
        self.assertIn("telemetry_error", res["telemetry"])


class KillSwitchTest(Gate):
    """Interruttore di emergenza (DECISIONS.md §15): spegne le nuove aperture senza
    toccare il prompt. Fail-closed, notifiche solo sulle transizioni."""

    def good(self):
        return self.emit(payload({a: ok_entry() for a in rg.ASSETS}))

    def sentinel_script(self):
        # Se il detector venisse lanciato, lascerebbe questa traccia su disco.
        self.sentinel = self.tmp / "detector_lanciato"
        return self.script(f"open({str(self.sentinel)!r}, 'w').write('x')\n"
                           f"print({json.dumps(json.dumps(payload({a: ok_entry() for a in rg.ASSETS})))})\n")

    def test_off_nulls_every_asset_and_never_launches_the_detector(self):
        self.set_switch("OFF\nmanutenzione Coinbase\n")
        res = self.gate(self.sentinel_script())
        self.assert_all_null(res, "kill_switch")
        self.assertEqual(res["per_asset"]["BTC"]["detail"], "manutenzione Coinbase")
        self.assertFalse(self.sentinel.exists())
        self.assertEqual(res["telemetry"]["outcome"], "kill_switch")
        self.assertEqual(res["telemetry"]["assets"], {a: "null:kill_switch" for a in rg.ASSETS})
        # Spegnimento voluto: nessun evento nel registro dei silenzi (budget intatto)
        self.assertFalse((self.tmp / rg.SILENCE_LOG).exists())

    def test_on_launches_the_detector(self):
        res = self.gate(self.sentinel_script())
        self.assertTrue(self.sentinel.exists())
        self.assertEqual(res["per_asset"]["BTC"]["tradable_regime"], "range")
        self.assertEqual(res["notifications"], [])

    def test_anything_but_on_is_fail_closed(self):
        cases = {
            "file assente": None,
            "file vuoto": "",
            "solo commenti": "# ON\n",
            "parola sbagliata": "ACCESO\n",
            "OFF con testo sulla stessa riga": "OFF - manutenzione\n",
            "ON con punteggiatura": "ON.\n",
            "byte non UTF-8": b"\xff\xfeON\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                if text is None:
                    (self.tmp / rg.SWITCH_FILE).unlink(missing_ok=True)
                else:
                    self.set_switch(text)
                res = self.gate(self.good())
                self.assert_all_null(res, "kill_switch_unreadable")
                self.assertEqual(len(res["notifications"]), 1)
                (self.tmp / rg.RUNS_LOG).unlink(missing_ok=True)   # ogni caso da zero

    def test_case_spaces_comments_and_bom_are_tolerated(self):
        for text in ("on\n", "  On  \r\n", "# commento\n\nON\n", "\ufeffON\n"):
            with self.subTest(text=text):
                self.set_switch(text)
                self.assertEqual(self.gate(self.good())["per_asset"]["BTC"]["tradable_regime"],
                                 "range")
        self.set_switch("# nota in testa\noff\n# commento\nmotivo\n")
        res = self.gate(self.good())
        self.assert_all_null(res, "kill_switch")
        self.assertEqual(res["per_asset"]["BTC"]["detail"], "motivo")

    def test_notifies_only_on_transitions(self):
        seen = []
        for text in ("ON", "OFF", "OFF", "ACCESO", "ACCESO", "ON", "ON"):
            self.set_switch(text + "\n")
            seen.append(self.gate(self.good())["notifications"])
        self.assertEqual(seen[0], [])
        self.assertEqual(len(seen[1]), 1)
        self.assertIn("OFF", seen[1][0])
        self.assertEqual(seen[2], [])                          # resta spento: silenzio
        self.assertEqual(len(seen[3]), 1)                      # OFF -> illeggibile: nuovo avviso
        self.assertIn("illeggibile", seen[3][0])
        self.assertEqual(seen[4], [])
        self.assertEqual(len(seen[5]), 1)                      # ripresa
        self.assertIn("ON", seen[5][0])
        self.assertEqual(seen[6], [])

    def test_off_notifies_even_if_the_previous_run_is_unreadable(self):
        (self.tmp / "logs").mkdir()
        (self.tmp / rg.RUNS_LOG).write_text("non json\n")
        self.set_switch("OFF\n")
        self.assertEqual(len(self.gate(self.good())["notifications"]), 1)

    def test_resume_note_comes_before_the_detector_notifications(self):
        self.set_switch("OFF\n")
        self.gate(self.good())
        self.set_switch("ON\n")
        res = self.gate(self.emit(payload({a: ok_entry() for a in rg.ASSETS},
                                          notifications=["dal detector"])))
        self.assertEqual(len(res["notifications"]), 2)
        self.assertIn("ON", res["notifications"][0])
        self.assertEqual(res["notifications"][1], "dal detector")

    def test_telemetry_chain_continues_through_the_switch(self):
        for text in ("ON", "OFF", "ON"):
            self.set_switch(text + "\n")
            self.gate(self.good())
        lines = [json.loads(l) for l in (self.tmp / rg.RUNS_LOG).read_text().splitlines()]
        self.assertEqual([l["outcome"] for l in lines], ["ok", "kill_switch", "ok"])
        self.assertIsNone(lines[0]["prev_run_ts"])
        self.assertEqual(lines[1]["prev_run_ts"], lines[0]["ts"])
        self.assertEqual(lines[2]["prev_run_ts"], lines[1]["ts"])

    def test_gate_error_keeps_the_telemetry_chain(self):
        self.gate(self.good())
        with mock.patch.object(rg, "read_switch", side_effect=MemoryError("finta")):
            res = rg.get_market_regime(base=self.tmp)
        self.assert_all_null(res, "gate_error")
        lines = [json.loads(l) for l in (self.tmp / rg.RUNS_LOG).read_text().splitlines()]
        self.assertEqual(lines[1]["prev_run_ts"], lines[0]["ts"])

    def test_committed_switch_file_is_well_formed(self):
        # Il file nel repo deve dire ON o OFF: un file malformato spegnerebbe il
        # trading per sbaglio. (Non si pretende ON: OFF e' uno stato legittimo.)
        reason, _ = rg.read_switch(Path(__file__).parent / rg.SWITCH_FILE)
        self.assertIn(reason, (None, "kill_switch"))


if __name__ == "__main__":
    unittest.main()
