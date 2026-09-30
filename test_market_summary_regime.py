"""market_summary.py --with-regime: integrazione del regime senza rompere nulla.

1. DIFFERENZIALE — senza --with-regime l'output e' identico alla versione
   precedente (copia congelata in test_fixtures/market_summary_legacy.py):
   stesso stdout, stderr ed exit code, su input valido e su input difettosi.
2. Con --with-regime l'output e' quello di prima PIU' i campi del regime, e
   togliendoli si ritorna esattamente all'output precedente.
3. Qualunque guasto del regime (gate che solleva, import impossibile) non cambia
   l'exit code: tutti gli asset ricevono tradable_regime null con reason.

Il gate e' sempre finto qui: nessuna rete, nessuna scrittura nei logs/ del repo.
"""

import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import market_summary                                  # noqa: E402
from test_market_summary import complete_market_data   # noqa: E402

REGIME_KEYS = ("regime_notifications", "regime_detector")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LEGACY = _load(ROOT / "test_fixtures" / "market_summary_legacy.py", "market_summary_legacy")


def _run(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = module.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _inputs(tmp: Path) -> dict:
    """Input validi e difettosi: il confronto deve reggere su tutti."""
    good = tmp / "good.json"
    good.write_text(json.dumps(complete_market_data()))
    bad_schema = tmp / "bad_schema.json"
    data = complete_market_data(); data["schema_version"] = 99
    bad_schema.write_text(json.dumps(data))
    broken = tmp / "broken.json"
    broken.write_text("{ non json")
    arg = lambda p: ["--input", str(p)]
    return {"valido": arg(good), "valido, 2 barre": arg(good) + ["--recent-bars", "2"],
            "schema errato": arg(bad_schema), "json rotto": arg(broken),
            "file assente": arg(tmp / "manca.json")}


def _strip_regime(summary: dict) -> dict:
    s = copy.deepcopy(summary)
    for k in REGIME_KEYS:
        s.pop(k, None)
    for block in s["assets"].values():
        block.pop("market_regime", None)
    return s


FAKE = {
    "per_asset": {
        "BTC": {"tradable_regime": "range", "state": "range", "confidence": 0.99,
                "reason": None, "detail": None, "as_of": "x", "min_confidence": 0.95},
        "ETH": {"tradable_regime": None, "state": "trend", "confidence": 0.9,
                "reason": "below_confidence_threshold", "detail": "sotto soglia",
                "as_of": "x", "min_confidence": 0.95},
        "SOL": {"tradable_regime": "trend", "state": "trend", "confidence": 0.97,
                "reason": None, "detail": None, "as_of": "x", "min_confidence": 0.95},
    },
    "notifications": ["🔇 prova"],
    "telemetry": {"outcome": "ok", "elapsed_s": 1.2},
}


class DifferentialTest(unittest.TestCase):
    def test_without_flag_output_is_identical_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, args in _inputs(Path(tmp)).items():
                with self.subTest(input=name):
                    self.assertEqual(_run(market_summary, args), _run(LEGACY, args))

    def test_without_flag_the_gate_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"regime_gate": None}):
            rc, out, _ = _run(market_summary, _inputs(Path(tmp))["valido"])
        self.assertEqual(rc, 0)
        self.assertNotIn("market_regime", out)

    def test_legacy_fixture_is_really_the_old_version(self):
        self.assertNotIn("with-regime", (ROOT / "test_fixtures" /
                                         "market_summary_legacy.py").read_text())


class WithRegimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.args = _inputs(Path(self.tmp.name))["valido"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_regime_and_nothing_else(self):
        fake_gate = mock.Mock(get_market_regime=mock.Mock(return_value=FAKE))
        with mock.patch.dict(sys.modules, {"regime_gate": fake_gate}):
            rc, out, err = _run(market_summary, self.args + ["--with-regime"])
        rc0, out0, err0 = _run(LEGACY, self.args)
        self.assertEqual((rc, err), (rc0, err0))
        new, old = json.loads(out), json.loads(out0)
        self.assertEqual(_strip_regime(new), old)                 # nient'altro cambia
        self.assertEqual(new["assets"]["BTC"]["market_regime"]["tradable_regime"], "range")
        self.assertIsNone(new["assets"]["ETH"]["market_regime"]["tradable_regime"])
        self.assertEqual(new["regime_notifications"], ["🔇 prova"])
        self.assertEqual(new["regime_detector"]["outcome"], "ok")

    def _assert_all_null(self, out, reason):
        new = json.loads(out)
        for asset in ("BTC", "ETH", "SOL"):
            mr = new["assets"][asset]["market_regime"]
            self.assertIsNone(mr["tradable_regime"])
            self.assertEqual(mr["reason"], reason)
        self.assertTrue(new["regime_notifications"])

    def test_gate_exception_never_breaks_the_summary(self):
        fake_gate = mock.Mock(get_market_regime=mock.Mock(side_effect=RuntimeError("giu'")))
        with mock.patch.dict(sys.modules, {"regime_gate": fake_gate}):
            rc, out, _ = _run(market_summary, self.args + ["--with-regime"])
        self.assertEqual(rc, 0)
        self._assert_all_null(out, "gate_unavailable")

    def test_unimportable_gate_never_breaks_the_summary(self):
        with mock.patch.dict(sys.modules, {"regime_gate": None}):     # import -> ImportError
            rc, out, _ = _run(market_summary, self.args + ["--with-regime"])
        self.assertEqual(rc, 0)
        self._assert_all_null(out, "gate_unavailable")

    def test_gate_missing_an_asset_gives_null_for_it(self):
        partial = copy.deepcopy(FAKE); partial["per_asset"].pop("SOL")
        fake_gate = mock.Mock(get_market_regime=mock.Mock(return_value=partial))
        with mock.patch.dict(sys.modules, {"regime_gate": fake_gate}):
            _, out, _ = _run(market_summary, self.args + ["--with-regime"])
        mr = json.loads(out)["assets"]["SOL"]["market_regime"]
        self.assertIsNone(mr["tradable_regime"])
        self.assertEqual(mr["reason"], "gate_unavailable")

    def test_invalid_market_data_still_fails_as_before(self):
        # Il regime non maschera un summary che deve fallire: exit 2 come prima.
        bad = _inputs(Path(self.tmp.name))["schema errato"]
        fake_gate = mock.Mock(get_market_regime=mock.Mock(return_value=FAKE))
        with mock.patch.dict(sys.modules, {"regime_gate": fake_gate}):
            rc, out, _ = _run(market_summary, bad + ["--with-regime"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        fake_gate.get_market_regime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
