"""regime_gate.py — ponte fra market_summary.py e regime_detector.py.

Unico scopo: ottenere dal detector un verdetto di regime per BTC/ETH/SOL SENZA
MAI rompere la routine (regime-training/DECISIONS.md §0, §11).

Perche' un processo separato
----------------------------
Il detector gira in un sottoprocesso con timeout DURO. E' l'unico modo di
garantire un tetto di tempo reale: una risoluzione DNS bloccata non rispetta i
timeout dei socket, e un bug (ciclo infinito, memoria) resterebbe confinato nel
figlio. Il detector ha anche un suo budget interno (20s) e di norma termina da
solo in ~1-2s: il timeout duro (25s) e' l'ultima cintura.

Garanzie
--------
- get_market_regime() NON solleva mai eccezioni.
- Restituisce SEMPRE un verdetto per ciascuno dei tre asset.
- Timeout, crash, output non JSON, output incoerente, exit code inatteso:
  tradable_regime = null con reason detector_<esito>. Mai un verdetto inventato.
- L'output del detector viene RIVALIDATO: un "tradable" con confidence sotto la
  propria soglia, o con stato diverso, viene scartato (detector_output_invalid).
- Anche i fallimenti di processo passano dal registro dei silenzi: si notifica
  all'inizio e alla ripresa, non a ogni run.
- Ogni esecuzione aggiunge una riga a logs/regime_runs.jsonl (telemetria):
  esito, tempo, verdetti, notifiche, e il timestamp della run PRECEDENTE trovata
  nel file — la prova, run dopo run, che il log persiste tra un container e
  l'altro.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DETECTOR = HERE / "regime_detector.py"
ASSETS = ("BTC", "ETH", "SOL")
STATES = ("range", "trend")
DETECTOR_DEADLINE_SECONDS = 20.0
HARD_TIMEOUT_SECONDS = 25.0
RUNS_LOG = Path("logs") / "regime_runs.jsonl"
SILENCE_LOG = Path("logs") / "regime_silence.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def null_verdict(reason: str, detail: str | None) -> dict:
    return {"tradable_regime": None, "state": None, "confidence": None,
            "reason": reason, "detail": detail, "as_of": None, "min_confidence": None}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_entry(entry) -> tuple[dict | None, str | None]:
    """Ritorna (verdetto normalizzato, None) oppure (None, motivo del rifiuto)."""
    if not isinstance(entry, dict):
        return None, "voce non oggetto"
    tradable, state = entry.get("tradable_regime"), entry.get("state")
    conf, reason = entry.get("confidence"), entry.get("reason")
    threshold = entry.get("min_confidence")
    if tradable not in (None, *STATES) or state not in (None, *STATES):
        return None, f"stato non ammesso: {tradable!r}/{state!r}"
    if conf is not None and not (_num(conf) and 0.0 <= conf <= 1.0):
        return None, f"confidence non valida: {conf!r}"
    if tradable is not None:
        if state != tradable or reason is not None:
            return None, "verdetto incoerente con lo stato"
        if not (_num(threshold) and _num(conf) and conf >= threshold):
            return None, "verdetto operativo senza confidence sopra soglia"
    elif not (isinstance(reason, str) and reason):
        return None, "verdetto nullo senza reason"
    return {"tradable_regime": tradable, "state": state, "confidence": conf,
            "reason": reason, "detail": entry.get("detail"),
            "as_of": entry.get("as_of"), "min_confidence": threshold}, None


def run_detector(*, python: str = sys.executable, script: Path = DETECTOR,
                 timeout: float = HARD_TIMEOUT_SECONDS,
                 deadline: float = DETECTOR_DEADLINE_SECONDS):
    """Esegue il detector. Ritorna (payload|None, esito, dettaglio, exit_code, secondi)."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run([python, str(script), "--deadline-seconds", str(deadline)],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout", f"nessuna risposta entro {timeout:.0f}s", None, time.monotonic() - t0
    except OSError as exc:
        return None, "launch_failed", str(exc), None, time.monotonic() - t0
    elapsed = time.monotonic() - t0
    err_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        outcome = "crashed" if proc.returncode else "invalid_output"
        return None, outcome, f"exit {proc.returncode}: {err_tail[0][:300]}", proc.returncode, elapsed
    # 0 = normale, 1 = integrita' fallita (output valido). Altro = anomalia.
    if proc.returncode not in (0, 1):
        return None, "crashed", f"exit code inatteso {proc.returncode}", proc.returncode, elapsed
    return payload, "ok", None, proc.returncode, elapsed


def _failure_notifications(base: Path, per_asset: dict, now: datetime) -> tuple[list[str], str | None]:
    """Notifiche per un fallimento di PROCESSO, con la stessa logica a transizioni
    del detector. Se non si puo' nemmeno usare il registro, un messaggio unico."""
    try:
        sys.path.insert(0, str(HERE))
        import regime_detector as rd
        reg = rd.SilenceRegister(base / SILENCE_LOG)
        notes = []
        for asset, verdict in per_asset.items():
            notes.extend(rd.update_register(reg, asset, verdict, now))
        reg.flush()
        return notes, reg.load_error
    except Exception as exc:  # noqa: BLE001 - doppio guasto: resta solo il messaggio
        reason = next(iter(per_asset.values()))["reason"]
        return [f"🛑 Regime non disponibile ({reason}); registro dei silenzi "
                f"inutilizzabile ({type(exc).__name__}). Nessuna nuova apertura in "
                f"questo ciclo. Gestione posizioni aperte invariata."], str(exc)


def _last_run_ts(path: Path) -> str | None:
    try:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if line:
                return json.loads(line).get("ts")
    except (OSError, ValueError, AttributeError):
        return None
    return None


def get_market_regime(*, base: Path = HERE, python: str = sys.executable,
                      script: Path = DETECTOR, timeout: float = HARD_TIMEOUT_SECONDS,
                      deadline: float = DETECTOR_DEADLINE_SECONDS,
                      write_telemetry: bool = True) -> dict:
    """{"per_asset": {asset: verdetto}, "notifications": [...], "telemetry": {...}}.

    Non solleva mai. Ogni asset ha sempre un verdetto.
    """
    now = _now()
    telemetry = {"ts": now.isoformat()}
    try:
        payload, outcome, detail, rc, elapsed = run_detector(
            python=python, script=script, timeout=timeout, deadline=deadline)
        per_asset, notifications = {}, []
        if outcome == "ok":
            if not (isinstance(payload, dict) and isinstance(payload.get("assets"), dict)):
                outcome, detail = "invalid_output", "struttura di output non riconosciuta"
            else:
                for asset in ASSETS:
                    verdict, why = _validate_entry(payload["assets"].get(asset))
                    per_asset[asset] = verdict or null_verdict("detector_output_invalid", why)
                notifications = [n for n in payload.get("notifications") or []
                                 if isinstance(n, str)]
                telemetry["register_events_loaded"] = payload.get("register_events_loaded")
                if payload.get("register_error"):
                    telemetry["register_error"] = payload["register_error"]
                if payload.get("integrity_ok") is False:
                    telemetry["integrity_errors"] = payload.get("integrity_errors")
        if outcome != "ok":
            per_asset = {a: null_verdict(f"detector_{outcome}", detail) for a in ASSETS}
            notifications, reg_err = _failure_notifications(base, per_asset, now)
            if reg_err:
                telemetry["register_error"] = reg_err
        telemetry.update({"outcome": outcome, "exit_code": rc,
                          "elapsed_s": round(elapsed, 3)})
    except Exception as exc:  # noqa: BLE001 - ultima cintura: mai un crash
        per_asset = {a: null_verdict("gate_error", f"{type(exc).__name__}: {exc}")
                     for a in ASSETS}
        notifications = [f"🛑 Regime non calcolabile (gate_error: {type(exc).__name__}). "
                         f"Nessuna nuova apertura in questo ciclo. Gestione posizioni "
                         f"aperte invariata."]
        telemetry.update({"outcome": "gate_error", "detail": str(exc)})

    telemetry["assets"] = {a: v["tradable_regime"] or f"null:{v['reason']}"
                           for a, v in per_asset.items()}
    telemetry["notifications"] = len(notifications)
    if write_telemetry:
        try:
            runs = base / RUNS_LOG
            telemetry["prev_run_ts"] = _last_run_ts(runs)
            runs.parent.mkdir(parents=True, exist_ok=True)
            with runs.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(telemetry, ensure_ascii=False) + "\n")
        except OSError as exc:
            telemetry["telemetry_error"] = str(exc)
    return {"per_asset": per_asset, "notifications": notifications, "telemetry": telemetry}
