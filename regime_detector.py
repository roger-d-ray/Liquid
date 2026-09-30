"""regime_detector.py — regime di mercato per la routine live (STEP 1.5).

Stampa su stdout un JSON con, per ogni asset, lo stato di regime e la confidence,
OPPURE l'assenza di regime con il motivo. Non apre posizioni, non tocca il conto,
non chiama Telegram: seguendo la convenzione di manage_positions.py e
intraday_exit.py, **Python decide e l'agente esegue**. Le notifiche da inviare
sono nel campo `notifications`; le manda la routine con telegram_notify.py.

Contratto di sicurezza (regime-training/DECISIONS.md §0)
-------------------------------------------------------
- Un filtro serve ad APRIRE, mai a PROTEGGERE. Se qui non esce un regime, la
  conseguenza ammessa e' UNA SOLA: nessun NUOVO trade in quel ciclo. Lo STEP 0
  (flatten, manage_positions, modifica SL, chiusure) non dipende da questo
  script e deve girare comunque.
- Fail-closed: se qualcosa non torna, nessun regime. Mai un regime "con
  confidence bassa", mai l'ultimo valore noto, mai una stima.

Verifiche di integrita' (tutte bloccanti, prima di qualsiasi rete)
-----------------------------------------------------------------
metadati presenti e leggibili; hash di regime_features.py / regime_hmm.py /
regime_source.py uguali a quelli registrati quando il codice e' stato USATO in
training; fonte dati e granularita' dichiarate uguali a quelle del modulo; nomi
e finestra delle feature uguali; per ogni asset: file modello presente, hash
combaciante, 2 stati, etichette uguali a quelle DERIVATE ora dai centroidi.
Se una sola fallisce: nessun regime per NESSUN asset, e nessuna chiamata di rete.

Registro dei silenzi
--------------------
logs/regime_silence.jsonl (append-only, tracciato in git e sincronizzato da
git_push_log.py come proposals.jsonl: il container si riclona a ogni run).
Serve a notificare UNA volta all'inizio del silenzio e UNA alla ripresa — non
ogni ora — e a sorvegliare il budget di silenzio su 90 giorni.
Il registro e' uno strumento di OSSERVAZIONE: la decisione fail-closed non
dipende mai da lui. Se il push fallisce e il registro si perde, il caso peggiore
e' una notifica ripetuta, non un trade sbagliato.

Contratto di output (per asset, SEMPRE presente per BTC/ETH/SOL)
-----------------------------------------------------------------
    tradable_regime   "range" | "trend" | null   <- l'UNICO campo su cui si decide
    state             stato del modello, anche se sotto soglia (diagnostico)
    confidence        posteriori filtrata dello stato (diagnostica)
    reason            null se tradable; altrimenti il codice del perche' no:
                      below_confidence_threshold, insufficient_history,
                      stale_data, stitch_error, source_unavailable,
                      detector_deadline, insufficient_feature_rows,
                      unlabelled_state, integrity_failed, no_model, detector_error
    detail            spiegazione leggibile di reason

La soglia di confidence (metadati: fail_safe.min_confidence, DECISIONS.md §10)
la applica QUESTO script: chi legge riceve un verdetto, non un numero da
confrontare. Confidence sotto soglia NON e' un silenzio del detector (lo stato
c'e', e' solo incerto): non entra nel registro dei silenzi — vedi update_register.

Tempo: budget interno (default 20s) applicato iniettando in regime_source un
http_get e uno sleep che rispettano la scadenza — regime_source.py, modulo
hashato, non viene modificato. Nessuna eccezione esce da run(): ogni asset
fallisce per conto suo, con reason.

Uso:
    python regime_detector.py            # JSON su stdout
    python regime_detector.py --asset BTC
    python regime_detector.py --no-register   # non scrive il registro (test)
    python regime_detector.py --deadline-seconds 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402
import regime_source as rs     # noqa: E402

META_PATH = HERE / "models" / "regime_model.meta.json"
REGISTER_PATH = HERE / "logs" / "regime_silence.jsonl"
MODULES = ("regime_features.py", "regime_hmm.py", "regime_source.py")
SCHEMA_VERSION = 1

# Sorveglianza del budget di silenzio (DECISIONS.md §4)
SILENCE_WINDOW_DAYS = 90
SILENCE_BUDGET_PCT = 5.0

DEFAULT_ASSETS = ("BTC", "ETH", "SOL")
DEFAULT_DEADLINE_SECONDS = 20.0
MIN_ASSET_BUDGET_SECONDS = 2.0     # sotto, non si inizia nemmeno l'asset


class DeadlineExceeded(Exception):
    """Budget di tempo esaurito. Volutamente NON un OSError: regime_source lo
    classifica come errore non transitorio e rinuncia subito, senza retry."""


def deadline_http_get(deadline: float):
    """http_get per regime_source che non supera la scadenza (time.monotonic()).

    Replica il trasporto di regime_source.default_http_get (stesso header, stessa
    normalizzazione degli errori HTTP) con timeout ridotto al tempo residuo. Il
    parsing delle righe resta in regime_source: qui c'e' solo trasporto.
    """
    def http_get(url: str):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            raise DeadlineExceeded("budget di tempo del detector esaurito")
        req = urllib.request.Request(url, headers={"User-Agent": "liquid-bot/1.0"})
        try:
            with urllib.request.urlopen(
                    req, timeout=min(rs.HTTP_TIMEOUT_SECONDS, remaining)) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise rs.HttpStatusError(exc.code, str(exc.reason)) from exc
    return http_get


def deadline_sleep(deadline: float):
    """Sleep che non va oltre la scadenza: la richiesta successiva poi rinuncia."""
    def sleep(seconds: float) -> None:
        time.sleep(max(0.0, min(seconds, deadline - time.monotonic())))
    return sleep


def _silent(reason: str, detail: str, **extra) -> dict:
    """Nessun regime: ne' stato ne' verdetto."""
    return {"tradable_regime": None, "state": None, "confidence": None,
            "reason": reason, "detail": detail, **extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Integrita' ───────────────────────────────────────────────────────────────
def verify_integrity(base: Path = HERE) -> tuple[dict | None, dict[str, dict], list[str]]:
    """Ritorna (meta, modelli_verificati, errori). Errori non vuoti = nessun regime.

    ``base`` parametrizza SOLO le letture da disco (metadati, modelli, hash dei
    moduli). I controlli semantici — SOURCE_ID, granularita', nomi delle feature —
    leggono i moduli IMPORTATI a inizio processo, non i file sotto ``base``. In
    produzione le due cose coincidono (``base`` e' la directory del bot, da cui i
    moduli sono stati importati), quindi il controllo e' corretto; ma un test che
    modifichi un modulo dentro una copia temporanea non cambia il modulo gia'
    importato. Quel caso e' comunque coperto: modificare il file fa fallire il
    confronto di hash.
    """
    errors: list[str] = []
    meta_path = base / "models" / "regime_model.meta.json"
    if not meta_path.exists():
        return None, {}, [f"metadati mancanti: {meta_path.name}"]
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, {}, [f"metadati illeggibili: {exc}"]

    if meta.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema metadati {meta.get('schema_version')}, atteso {SCHEMA_VERSION}")

    # Le soglie fail-safe DEVONO esserci: mai un default silenzioso, soprattutto
    # sulla soglia di confidence (senza, ogni stato diventerebbe operativo).
    fs = meta.get("fail_safe") or {}
    for key in ("min_feature_rows", "min_contiguous_bars", "target_contiguous_bars"):
        if not isinstance(fs.get(key), int) or fs[key] <= 0:
            errors.append(f"fail_safe.{key} mancante o non valido")
    mc = fs.get("min_confidence")
    if not isinstance(mc, (int, float)) or isinstance(mc, bool) or not 0.5 < mc <= 1.0:
        errors.append(f"fail_safe.min_confidence mancante o fuori da (0,5 ; 1]: {mc!r}")

    # Il codice e' quello con cui il modello e' stato addestrato?
    for module, expected in (meta.get("modules") or {}).items():
        path = base / module
        if not path.exists():
            errors.append(f"modulo mancante: {module}")
        elif sha256_file(path) != expected:
            errors.append(f"{module} non combacia con l'hash del training")
    for module in MODULES:
        if module not in (meta.get("modules") or {}):
            errors.append(f"hash non registrato per {module}")

    # La fonte dati e' quella del training?
    if meta.get("data_source") != rs.SOURCE_ID:
        errors.append(f"fonte del modello '{meta.get('data_source')}' != fonte live "
                      f"'{rs.SOURCE_ID}'")
    if meta.get("granularity_seconds") != rs.GRANULARITY_SECONDS:
        errors.append("granularita' del modello diversa da quella della fonte live")
    if meta.get("feature_names") != list(rf.FEATURE_NAMES):
        errors.append("feature del modello diverse da quelle del codice")
    if meta.get("feature_window_bars") != rf.FEATURE_WINDOW_BARS:
        errors.append("finestra feature del modello diversa da quella del codice")

    models: dict[str, dict] = {}
    for asset, info in (meta.get("models") or {}).items():
        path = base / info["file"]
        if not path.exists():
            errors.append(f"[{asset}] modello mancante: {info['file']}")
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != info.get("sha256"):
            errors.append(f"[{asset}] {info['file']} alterato rispetto ai metadati")
            continue
        try:
            model = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"[{asset}] modello illeggibile: {exc}")
            continue
        if model.get("n_states") != meta.get("n_states"):
            errors.append(f"[{asset}] n_states del modello != metadati")
            continue
        # Etichette RI-DERIVATE ora: un retraining che permuta gli stati resta corretto
        try:
            derived = regime_hmm.derive_labels(model)
        except regime_hmm.LabelError as exc:
            errors.append(f"[{asset}] etichettatura impossibile: {exc}")
            continue
        if derived != model.get("labels"):
            errors.append(f"[{asset}] etichette salvate != derivate dai centroidi")
            continue
        models[asset] = model
    if not models and not errors:
        errors.append("nessun modello dichiarato nei metadati")
    return meta, models, errors


# ── Feature sulle barre appena scaricate ────────────────────────────────────
def feature_rows(bars: list[dict]) -> list[list[float]]:
    """Una riga di feature per ogni finestra canonica completa.

    Le barre arrivano gia' contigue e chiuse da regime_source.fetch_recent_bars,
    quindi ogni finestra e' valida: nessun controllo di contiguita' duplicato qui.
    """
    w = rf.FEATURE_WINDOW_BARS
    rows = []
    for end in range(w - 1, len(bars)):
        f = rf.compute_features(bars[end - w + 1: end + 1])
        if f is None:
            return []          # una finestra incalcolabile invalida la sequenza
        rows.append([f[n] for n in rf.FEATURE_NAMES])
    return rows


def _recovery_estimate(asset: str, min_bars: int, target_bars: int,
                       now: float | None = None, http_get=None,
                       sleep=None) -> dict | None:
    """Stima quando ripartira' il regime dopo un buco della fonte.

    Diagnostica BEST-EFFORT, eseguita solo quando siamo GIA' in rifiuto: misura
    la coda contigua attuale e calcola quante ore mancano a min_bars. Non tocca
    il percorso di sicurezza e, se fallisce, la notifica esce senza stima.
    """
    clock = time.time() if now is None else now
    try:
        end = rs.last_closed_open_time(clock)
        first = end - (target_bars - 1) * rs.GRANULARITY_SECONDS
        kwargs = {k: v for k, v in (("http_get", http_get), ("sleep", sleep)) if v}
        collected, _ = rs.fetch_range(asset, first, end, **kwargs)
    except Exception:
        return None
    tail, t = 0, end
    while t >= first and t in collected:
        tail += 1
        t -= rs.GRANULARITY_SECONDS
    missing = max(min_bars - tail, 0)
    gap_at = t if tail and t >= first else None
    return {
        "contiguous_bars": tail,
        "bars_missing": missing,
        "expected_recovery": _iso(
            datetime.fromtimestamp(clock, tz=timezone.utc) + timedelta(hours=missing)),
        "gap_ends_at": _iso(datetime.fromtimestamp(gap_at, tz=timezone.utc))
        if gap_at else None,
    }


def detect_asset(asset: str, model: dict, fail_safe: dict,
                 now: float | None = None, http_get=None, sleep=None) -> dict:
    """Verdetto per un asset: tradable_regime, oppure null con reason.

    ``now`` (epoch secondi) rende il risultato deterministico nei test; in
    produzione run() passa l'istante di inizio run. ``http_get``/``sleep``
    arrivano da run() gia' vincolati alla scadenza.
    """
    min_bars = fail_safe["min_contiguous_bars"]
    target = fail_safe["target_contiguous_bars"]
    min_rows = fail_safe["min_feature_rows"]
    threshold = fail_safe["min_confidence"]
    io = {k: v for k, v in (("http_get", http_get), ("sleep", sleep)) if v}
    try:
        bars = rs.fetch_recent_bars(asset, min_bars=min_bars, target_bars=target,
                                    now=now, **io)
    except rs.InsufficientHistory as exc:
        out = _silent("insufficient_history", str(exc))
        est = _recovery_estimate(asset, min_bars, target, now=now, **io)
        if est:
            out.update(est)
        return out
    except rs.StaleData as exc:
        return _silent("stale_data", str(exc))
    except rs.StitchError as exc:
        return _silent("stitch_error", str(exc))
    except rs.SourceError as exc:
        if isinstance(exc.__cause__, DeadlineExceeded):
            return _silent("detector_deadline", str(exc))
        return _silent("source_unavailable", str(exc))

    rows = feature_rows(bars)
    if len(rows) < min_rows:
        return _silent("insufficient_feature_rows",
                       f"{len(rows)} righe di feature, minimo {min_rows}")

    # Posteriori FILTRATA (forward): mai Viterbi, che userebbe il futuro.
    result = regime_hmm.predict_regime(model, rows)
    if result["label"] is None:
        return _silent("unlabelled_state", f"stato {result['state']} senza etichetta")
    last_open = bars[-1]["open_time_ms"]
    info = {
        "as_of": _iso(datetime.fromtimestamp(last_open / 1000, tz=timezone.utc)),
        "bars_used": len(bars),
        "feature_rows": len(rows),
        "min_confidence": threshold,
    }
    confidence = result["confidence"]
    if confidence >= threshold:
        return {"tradable_regime": result["label"], "state": result["label"],
                "confidence": confidence, "reason": None, "detail": None, **info}
    return {"tradable_regime": None, "state": result["label"], "confidence": confidence,
            "reason": "below_confidence_threshold",
            "detail": f"confidence {confidence:.4f} sotto la soglia {threshold}", **info}


# ── Registro dei silenzi ────────────────────────────────────────────────────
class SilenceRegister:
    """Eventi append-only di inizio/fine silenzio e superamento del budget.

    Tollerante ai danni: una riga illeggibile viene saltata invece di far
    fallire il detector. Il registro osserva, non decide.
    """

    def __init__(self, path: Path = REGISTER_PATH):
        self.path = path
        self.events: list[dict] = []
        self.load_error: str | None = None
        text = ""
        if path.exists():
            # Un registro illeggibile (permessi, byte corrotti, percorso sbagliato)
            # diventa un registro VUOTO con errore segnalato: il detector non deve
            # mai cadere per il registro, che osserva e non decide. Costo: al
            # peggio una notifica ripetuta.
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.load_error = f"registro illeggibile: {exc}"
        if text:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not (isinstance(event, dict) and event.get("ts") and event.get("asset")):
                    continue
                try:
                    datetime.fromisoformat(event["ts"])
                except (TypeError, ValueError):
                    continue
                self.events.append(event)
        # Ordine per TIMESTAMP, non per posizione nel file. Il fallback API di
        # git_push_log.py unisce accodando le righe locali mancanti su main: con
        # due run sovrapposti un evento potrebbe finire fuori ordine, e lo stato
        # dedotto dall'ordine del file sarebbe sbagliato. Ordinamento stabile:
        # a parita' di timestamp resta l'ordine di scrittura.
        self.events.sort(key=lambda e: datetime.fromisoformat(e["ts"]))
        self.pending: list[dict] = []

    def _for(self, asset: str, kinds: tuple[str, ...]) -> list[dict]:
        return [e for e in self.events if e["asset"] == asset and e.get("event") in kinds]

    def is_silent(self, asset: str) -> bool:
        seq = self._for(asset, ("silence_start", "silence_end"))
        return bool(seq) and seq[-1]["event"] == "silence_start"

    def budget_flagged(self, asset: str) -> bool:
        seq = self._for(asset, ("budget_exceeded", "budget_recovered"))
        return bool(seq) and seq[-1]["event"] == "budget_exceeded"

    def append(self, event: dict) -> None:
        self.events.append(event)
        self.pending.append(event)

    def silence_fraction(self, asset: str, now: datetime,
                         window_days: int = SILENCE_WINDOW_DAYS) -> float:
        """Frazione di tempo in silenzio nella finestra recente.

        Un silenzio ancora aperto conta fino ad ADESSO; gli intervalli vengono
        ritagliati sulla finestra, cosi' un silenzio iniziato prima conta solo
        per la parte che ricade dentro.
        """
        start = now - timedelta(days=window_days)
        total, open_at = 0.0, None
        for event in self._for(asset, ("silence_start", "silence_end")):
            try:
                ts = datetime.fromisoformat(event["ts"])
            except ValueError:
                continue
            if event["event"] == "silence_start":
                open_at = open_at or ts
            elif open_at is not None:
                total += max((min(ts, now) - max(open_at, start)).total_seconds(), 0)
                open_at = None
        if open_at is not None:
            total += max((now - max(open_at, start)).total_seconds(), 0)
        return total / (window_days * 86400) * 100

    def flush(self) -> None:
        if not self.pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for event in self.pending:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.pending.clear()


def update_register(register: SilenceRegister, asset: str, result: dict,
                    now: datetime) -> list[str]:
    """Aggiorna il registro sulle TRANSIZIONI e ritorna le notifiche da mandare.

    Solo le transizioni producono eventi e notifiche: un silenzio che continua
    non notifica nulla, cosi' non si riceve un messaggio ogni ora.
    """
    notes: list[str] = []
    # Silenzio = NESSUNO stato (fonte, integrita', tempo...). Una confidence sotto
    # soglia NON e' silenzio: lo stato c'e', e' solo incerto, e capita nel 15-18%
    # delle ore concentrato sulle transizioni. Contarla qui significherebbe
    # notificare a ogni cambio di regime e sforare subito il budget del 5%, che e'
    # pensato per i buchi della fonte (DECISIONS.md §4, §10).
    silent_now = result.get("state") is None
    was_silent = register.is_silent(asset)

    if silent_now and not was_silent:
        event = {"ts": _iso(now), "asset": asset, "event": "silence_start",
                 "reason": result.get("reason"), "detail": result.get("detail")}
        for key in ("expected_recovery", "contiguous_bars", "bars_missing", "gap_ends_at"):
            if key in result:
                event[key] = result[key]
        register.append(event)
        icon = "🛑" if result.get("reason") == "integrity_failed" else "🔇"
        msg = (f"{icon} Regime non disponibile — {asset}\n"
               f"Causa: {result.get('reason')} — {result.get('detail')}")
        if result.get("gap_ends_at"):
            msg += f"\nBuco della fonte fino a: {result['gap_ends_at']}"
        if result.get("expected_recovery"):
            msg += (f"\nRipresa prevista: {result['expected_recovery']}"
                    f" ({result.get('bars_missing')} ore)")
        msg += "\nNessun NUOVO trade su questo asset. Gestione posizioni aperte invariata."
        notes.append(msg)
    elif not silent_now and was_silent:
        register.append({"ts": _iso(now), "asset": asset, "event": "silence_end"})
        tail = ("" if result.get("tradable_regime")
                else " — sotto soglia: per ora nessuna nuova apertura")
        notes.append(f"🔊 Regime di nuovo disponibile — {asset}: "
                     f"{result['state']} (confidence {result['confidence']:.2f}){tail}")

    # Budget di silenzio su finestra mobile: una notifica al superamento, una al rientro.
    pct = register.silence_fraction(asset, now)
    if pct > SILENCE_BUDGET_PCT and not register.budget_flagged(asset):
        register.append({"ts": _iso(now), "asset": asset, "event": "budget_exceeded",
                         "window_days": SILENCE_WINDOW_DAYS, "silence_pct": round(pct, 2)})
        notes.append(f"⚠️ Budget di silenzio superato — {asset}: {pct:.1f}% del tempo "
                     f"negli ultimi {SILENCE_WINDOW_DAYS} giorni (soglia "
                     f"{SILENCE_BUDGET_PCT}%). Da rivedere la finestra delle feature "
                     f"(regime-training/DECISIONS.md §4).")
    elif pct <= SILENCE_BUDGET_PCT and register.budget_flagged(asset):
        register.append({"ts": _iso(now), "asset": asset, "event": "budget_recovered",
                         "window_days": SILENCE_WINDOW_DAYS, "silence_pct": round(pct, 2)})
        notes.append(f"✅ Budget di silenzio rientrato — {asset}: {pct:.1f}%")
    return notes


# ── Orchestrazione ──────────────────────────────────────────────────────────
def run(assets: list[str] | None = None, *, use_register: bool = True,
        base: Path = HERE, deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        http_get=None, sleep=None) -> dict:
    """Esegue il detector. Non solleva: ogni problema diventa una reason.

    Integrita' fallita -> ogni asset null con reason integrity_failed e NESSUNA
    chiamata di rete. Anche questo passa dal registro: si notifica all'inizio e
    alla ripresa, non a ogni run.
    """
    t0 = time.monotonic()
    deadline = t0 + deadline_seconds
    now = _now()
    meta, models, errors = verify_integrity(base)
    out = {"schema_version": SCHEMA_VERSION, "generated_at": _iso(now),
           "assets": {}, "notifications": [], "integrity_ok": not errors,
           "min_confidence": ((meta or {}).get("fail_safe") or {}).get("min_confidence")}
    if errors:
        out["integrity_errors"] = errors
    hg = http_get or deadline_http_get(deadline)
    sl = sleep or deadline_sleep(deadline)

    register = None
    if use_register:
        register = SilenceRegister(base / "logs" / "regime_silence.jsonl")
        out["register_events_loaded"] = len(register.events)
        if register.load_error:
            out["register_error"] = register.load_error

    for asset in (assets or list(DEFAULT_ASSETS)):
        if errors:
            result = _silent("integrity_failed", "; ".join(errors))
        elif asset not in models:
            result = _silent("no_model", f"nessun modello verificato per {asset}")
        elif deadline - time.monotonic() < MIN_ASSET_BUDGET_SECONDS:
            result = _silent("detector_deadline",
                             "budget di tempo esaurito prima di iniziare l'asset")
        else:
            try:
                result = detect_asset(asset, models[asset], meta["fail_safe"],
                                      now=now.timestamp(), http_get=hg, sleep=sl)
            except Exception as exc:  # noqa: BLE001 - fail-closed per asset
                result = _silent("detector_error", f"{type(exc).__name__}: {exc}")
        out["assets"][asset] = result
        if register is not None:
            out["notifications"].extend(update_register(register, asset, result, now))

    if register is not None:
        try:
            register.flush()
        except OSError as exc:
            prev = out.get("register_error")
            out["register_error"] = (prev + " · " if prev else "") + f"registro non scritto: {exc}"
        out["silence_pct_90d"] = {a: round(register.silence_fraction(a, now), 2)
                                  for a in out["assets"]}
    out["elapsed_seconds"] = round(time.monotonic() - t0, 3)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Regime di mercato per la routine live")
    p.add_argument("--asset", action="append", dest="assets",
                   help="limita agli asset indicati (ripetibile)")
    p.add_argument("--no-register", action="store_true",
                   help="non leggere/scrivere il registro dei silenzi")
    p.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS,
                   help="budget di tempo interno (default %(default)s s)")
    args = p.parse_args(argv)
    result = run(args.assets, use_register=not args.no_register,
                 deadline_seconds=args.deadline_seconds)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    # 1 solo se l'integrita' e' compromessa (serve un umano). Il silenzio di un
    # asset e' un esito legittimo, non un errore dello script.
    return 0 if result["integrity_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
