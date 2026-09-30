"""Test di git_push_log.py.

1. DIFFERENZIALE — la chiamata senza argomenti e' identica alla versione
   precedente. La vecchia versione e' congelata byte per byte in
   test_fixtures/git_push_log_legacy.py; per ogni scenario si eseguono entrambe
   con lo stesso ambiente finto e si confrontano TUTTI gli effetti: comandi git
   (argomenti esatti), richieste HTTP (metodo, URL, header, corpo), stdout,
   stderr ed exit code. Rete e git sono sempre finti: nessun push reale.
2. --also — log aggiuntivi: proposals.jsonl resta trattato come prima,
   il file extra viene aggiunto, un file assente viene saltato, i percorsi
   pericolosi vengono rifiutati.

    python3 -m unittest test_git_push_log -v
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
FIXED = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
TOKEN = "ghp_finto_per_test"
PROPOSALS = '{"ts": "2026-09-30T10:00:00Z", "result": "no_setup"}\n'
SILENCE = '{"ts": "2026-09-30T11:00:00+00:00", "asset": "BTC", "event": "silence_start"}\n'


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED if tz else FIXED.replace(tzinfo=None)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class World:
    """Ambiente finto: git, API GitHub, variabili d'ambiente, directory di lavoro.

    scenario = {
      "git":   {"commit": rc, "push": rc},           # rc dei sottocomandi git
      "api":   {("GET"|"PUT", "<path>"): payload|status_int},
      "token": str|None,  "dotenv": str|None,
      "files": {"logs/x.jsonl": "contenuto"},
    }
    """

    def __init__(self, scenario):
        self.s = scenario
        self.cmds, self.http = [], []

    def _run(self, cmd, *a, check=False, **k):
        self.cmds.append(list(cmd))
        rc = self.s.get("git", {}).get(cmd[1], 0) if len(cmd) > 1 else 0
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd)
        return subprocess.CompletedProcess(cmd, rc)

    def _urlopen(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.http.append({"method": req.get_method(), "url": req.full_url,
                          "headers": sorted(req.header_items()), "body": body})
        path = req.full_url.split("/contents/", 1)[1].split("?", 1)[0]
        resp = self.s.get("api", {}).get((req.get_method(), path), 404)
        if isinstance(resp, int):
            raise urllib.error.HTTPError(req.full_url, resp, "errore finto", {},
                                         io.BytesIO(b'{"message": "finto"}'))
        return _Resp(resp)

    def execute(self, module_path: Path, argv=None):
        """Carica il modulo, lo esegue nel mondo finto, ritorna tutti gli effetti."""
        self.cmds, self.http = [], []
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for rel, text in self.s.get("files", {}).items():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_text(text)
            if self.s.get("dotenv") is not None:
                (tmp / ".env").write_text(self.s["dotenv"])
            mod = _load(module_path, f"gpl_{abs(hash(str(module_path)))}")
            mod.__file__ = str(tmp / "git_push_log.py")    # .env cercato qui
            mod.datetime = _Frozen
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
            if self.s.get("token"):
                env["GITHUB_TOKEN"] = self.s["token"]
            out, err = io.StringIO(), io.StringIO()
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch("subprocess.run", self._run), \
                        mock.patch("urllib.request.urlopen", self._urlopen), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = mod.main() if argv is None else mod.main(argv)
            finally:
                os.chdir(cwd)
        return {"rc": rc, "cmds": self.cmds, "http": self.http,
                "stdout": out.getvalue(), "stderr": err.getvalue()}


def _remote(text):
    return {"sha": "abc123", "content": base64.b64encode(text.encode()).decode()}


P = "logs/proposals.jsonl"
S = "logs/regime_silence.jsonl"
SCENARIOS = {
    "push git riuscito (SSH locale, senza token)":
        {"files": {P: PROPOSALS}},
    "push fallito, API: file remoto esistente, righe nuove":
        {"token": TOKEN, "git": {"push": 1}, "files": {P: PROPOSALS},
         "api": {("GET", P): _remote('{"vecchia": 1}\n'), ("PUT", P): {}}},
    "push fallito, API: file remoto assente (404) -> creazione":
        {"token": TOKEN, "git": {"push": 1}, "files": {P: PROPOSALS},
         "api": {("GET", P): 404, ("PUT", P): {}}},
    "push fallito, API: niente di nuovo da scrivere":
        {"token": TOKEN, "git": {"push": 1}, "files": {P: PROPOSALS},
         "api": {("GET", P): _remote(PROPOSALS)}},
    "push fallito, API: GET 500":
        {"token": TOKEN, "git": {"push": 1}, "files": {P: PROPOSALS},
         "api": {("GET", P): 500}},
    "push fallito, API: PUT 409":
        {"token": TOKEN, "git": {"push": 1}, "files": {P: PROPOSALS},
         "api": {("GET", P): _remote(""), ("PUT", P): 409}},
    "push fallito, nessun token":
        {"git": {"push": 1}, "files": {P: PROPOSALS}},
    "niente da committare, push riuscito":
        {"git": {"commit": 1}, "files": {P: PROPOSALS}},
    "token letto da .env":
        {"dotenv": f"GITHUB_TOKEN={TOKEN}\n", "git": {"push": 1}, "files": {P: PROPOSALS},
         "api": {("GET", P): _remote(""), ("PUT", P): {}}},
}

LEGACY = ROOT / "test_fixtures" / "git_push_log_legacy.py"
CURRENT = ROOT / "git_push_log.py"


class DifferentialTest(unittest.TestCase):
    def test_no_argument_call_is_identical_to_legacy(self):
        for name, scenario in SCENARIOS.items():
            with self.subTest(scenario=name):
                old = World(scenario).execute(LEGACY)
                new = World(scenario).execute(CURRENT, argv=[])
                self.assertEqual(new, old)

    def test_legacy_fixture_is_really_the_old_version(self):
        # La copia congelata non deve contenere --also: se qualcuno la
        # "aggiornasse", il test differenziale non proverebbe piu' nulla.
        text = LEGACY.read_text()
        self.assertNotIn("--also", text)
        self.assertIn('LOG_PATH = "logs/proposals.jsonl"', text)


class AlsoTest(unittest.TestCase):
    def test_extra_log_is_staged_with_proposals(self):
        res = World({"files": {P: PROPOSALS, S: SILENCE}}).execute(CURRENT, ["--also", S])
        self.assertIn(["git", "add", P, S], res["cmds"])
        self.assertEqual(res["rc"], 0)

    def test_api_path_syncs_proposals_exactly_as_before_then_extra(self):
        api = {("GET", P): _remote('{"vecchia": 1}\n'), ("PUT", P): {},
               ("GET", S): 404, ("PUT", S): {}}
        base = {"token": TOKEN, "git": {"push": 1}, "api": api}
        old = World({**base, "files": {P: PROPOSALS}}).execute(LEGACY)
        new = World({**base, "files": {P: PROPOSALS, S: SILENCE}}).execute(
            CURRENT, ["--also", S])
        n = len(old["http"])
        self.assertEqual(new["http"][:n], old["http"])     # proposals: identico
        extra = new["http"][n:]
        self.assertEqual([h["method"] for h in extra], ["GET", "PUT"])
        self.assertTrue(all(h["url"].split("?")[0].endswith(S) for h in extra))
        written = base64.b64decode(extra[1]["body"]["content"]).decode()
        self.assertEqual(written, SILENCE)
        self.assertEqual(new["rc"], 0)

    def test_missing_extra_log_is_skipped_and_proposals_unchanged(self):
        scenario = {"token": TOKEN, "git": {"push": 1}, "files": {P: PROPOSALS},
                    "api": {("GET", P): _remote(""), ("PUT", P): {}}}
        old = World(scenario).execute(LEGACY)
        new = World(scenario).execute(CURRENT, ["--also", S])
        self.assertEqual(new["cmds"], old["cmds"])
        self.assertEqual(new["http"], old["http"])
        self.assertEqual(new["rc"], old["rc"])
        self.assertIn("assente", new["stdout"])

    def test_extra_log_failure_is_reported(self):
        api = {("GET", P): _remote(""), ("PUT", P): {}, ("GET", S): 404, ("PUT", S): 409}
        res = World({"token": TOKEN, "git": {"push": 1}, "api": api,
                     "files": {P: PROPOSALS, S: SILENCE}}).execute(CURRENT, ["--also", S])
        self.assertEqual(res["rc"], 1)                     # la perdita e' visibile
        self.assertIn("ERRORE", res["stderr"])

    def test_dangerous_paths_are_refused(self):
        for bad in (".env", "logs/../.env", "/etc/passwd", "logs/x.txt",
                    "logs/sub/x.jsonl", "data/portfolio_state.json"):
            with self.subTest(path=bad):
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    _load(CURRENT, "gpl_guard")._parse_args(["--also", bad])

    def test_proposals_passed_as_extra_is_not_duplicated(self):
        res = World({"files": {P: PROPOSALS}}).execute(CURRENT, ["--also", P])
        self.assertIn(["git", "add", P], res["cmds"])


if __name__ == "__main__":
    unittest.main()
