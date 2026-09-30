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


if __name__ == "__main__":
    unittest.main()
