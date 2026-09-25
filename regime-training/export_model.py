"""export_model.py — Passo 5: porta modello e codice nel bot, con prova d'integrita'.

Unico passaggio training -> bot. Mai copia manuale. Cosa scrive nel bot:

  models/regime_{ASSET}.json      parametri + etichette + provenienza (per asset)
  models/regime_model.meta.json   contratto: hash di tutto, fonte, soglie fail-safe
  regime_features.py              copia VERBATIM (feature)
  regime_hmm.py                   copia VERBATIM (scorer + regola etichette)
  regime_source.py                copia VERBATIM (fonte dati live)

Tutto questo va COMMITTATO in git: il container cloud si riclona a ogni run, e
cio' che resta in artifacts/ (git-ignored) sparirebbe. DECISIONS.md §7.

Verifiche BLOCCANTI (l'export rifiuta, non avvisa):
  - il modello e' a 2 stati (DECISIONS §1)
  - regime_features.py attuale == quello con cui e' stato costruito il dataset
  - regime_hmm.py attuale == quello con cui e' stata verificata la parita'
  - regime_source.py attuale == quello con cui e' stato scaricato lo storico,
    e fonte/granularita' coincidono con quelle dichiarate dal modulo
  - parita' scorer puro vs hmmlearn entro soglia, 0 disallineamenti Viterbi
  - convergenza del filtraggio compatibile con la soglia minima del fail-safe
  - etichette = quelle derivate ora dalla regola sui centroidi
  - parametri numericamente sani
Dopo la scrittura rilegge i file dal disco e ne ricontrolla gli hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
BOT = HERE.parent
MODELS = BOT / "models"
sys.path.insert(0, str(HERE))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402
import regime_source as rs     # noqa: E402

ASSETS = ("BTC", "ETH", "SOL")
MODULES = ("regime_features.py", "regime_hmm.py", "regime_source.py")
N_STATES = 2                        # DECISIONS.md §1

# Fail-safe (DECISIONS.md §4). Misurati sul modello a 2 stati.
MIN_FEATURE_ROWS = 50
CONVERGENCE_SAFETY = 1.5            # MIN deve essere >= 1,5 x convergenza misurata
TARGET_CONTIGUOUS_BARS = rs.PAGE_BARS    # una sola richiesta per asset in live
MIN_CONTIGUOUS_BARS = rf.FEATURE_WINDOW_BARS + MIN_FEATURE_ROWS - 1

PARITY_MAX_PROBA_DIFF = 1e-6
PARITY_MAX_LOGLIK_DIFF = 1e-6
META_SCHEMA_VERSION = 1


class ExportRefused(RuntimeError):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def _finite(x) -> bool:
    if isinstance(x, list):
        return all(_finite(v) for v in x)
    return isinstance(x, (int, float)) and math.isfinite(x)


def check_artifact(asset: str, art: dict, module_sha: dict) -> None:
    def refuse(msg):
        raise ExportRefused(f"[{asset}] {msg}")

    if art.get("n_states") != N_STATES:
        refuse(f"n_states={art.get('n_states')}, la decisione e' {N_STATES} (DECISIONS §1)")
    if art.get("features_module_sha256") != module_sha["regime_features.py"]:
        refuse("regime_features.py e' cambiato dopo il build del dataset: "
               "rilancia build_dataset.py e train_model.py")
    if art.get("scorer_module_sha256") != module_sha["regime_hmm.py"]:
        refuse("regime_hmm.py e' cambiato dopo la verifica di parita': rilancia train_model.py")
    prov = art.get("provenance") or {}
    if prov.get("data_source") != rs.SOURCE_ID:
        refuse(f"modello addestrato su '{prov.get('data_source')}', la fonte live e' "
               f"'{rs.SOURCE_ID}': riaddestra sulla fonte live")
    if prov.get("granularity_seconds") != rs.GRANULARITY_SECONDS:
        refuse("granularita' di training diversa da quella live")
    if prov.get("source_module_sha256") != module_sha["regime_source.py"]:
        refuse("regime_source.py e' cambiato dopo il fetch dello storico: "
               "rilancia fetch_history.py e la pipeline")
    if art.get("feature_names") != list(rf.FEATURE_NAMES):
        refuse("feature del modello diverse da quelle del codice")
    if art.get("feature_window_bars") != rf.FEATURE_WINDOW_BARS:
        refuse("finestra feature del modello diversa da quella del codice")

    par = art.get("parity") or {}
    if (par.get("viterbi_mismatches") != 0
            or par.get("proba_max_abs_diff", 1) > PARITY_MAX_PROBA_DIFF
            or par.get("loglik_abs_diff", 1) > PARITY_MAX_LOGLIK_DIFF):
        refuse(f"parita' scorer non dimostrata: {par}")

    conv = (art.get("convergence") or {}).get("rows_needed")
    if conv is None:
        refuse("convergenza del filtraggio non raggiunta nella griglia misurata")
    if conv * CONVERGENCE_SAFETY > MIN_FEATURE_ROWS:
        refuse(f"il modello converge a {conv} righe: con margine {CONVERGENCE_SAFETY}x "
               f"servirebbero {math.ceil(conv * CONVERGENCE_SAFETY)} > minimo "
               f"{MIN_FEATURE_ROWS}. Rivedere la soglia (DECISIONS §4), non forzare.")

    try:
        derived = regime_hmm.derive_labels(art)
    except regime_hmm.LabelError as exc:
        refuse(f"etichettatura impossibile: {exc}")
    if derived != art.get("labels"):
        refuse(f"etichette salvate {art.get('labels')} != derivate {derived}")

    for key in ("startprob", "transmat", "means", "vars", "scaler_mean", "scaler_std"):
        if not _finite(art.get(key)):
            refuse(f"parametro non finito: {key}")
    if min(art["scaler_std"]) <= 0 or min(min(v) for v in art["vars"]) <= 0:
        refuse("varianze non positive")
    rows = [art["startprob"], *art["transmat"]]
    if any(abs(sum(r) - 1) > 1e-9 for r in rows):
        refuse("probabilita' che non sommano a 1")


def main() -> int:
    module_sha = {m: sha256_file(HERE / m) for m in MODULES}
    if TARGET_CONTIGUOUS_BARS < MIN_CONTIGUOUS_BARS:
        print("RIFIUTATO: target di fetch inferiore al minimo del fail-safe")
        return 1

    staged: dict[Path, bytes] = {}
    models_meta = {}
    try:
        for asset in ASSETS:
            src = HERE / "artifacts" / f"{asset}_hmm_{N_STATES}.json"
            if not src.exists():
                raise ExportRefused(f"[{asset}] artefatto mancante: {src.name}")
            art = json.loads(src.read_text())
            check_artifact(asset, art, module_sha)
            data = (json.dumps(art, indent=2, sort_keys=True) + "\n").encode()
            rel = f"models/regime_{asset}.json"
            staged[BOT / rel] = data
            models_meta[asset] = {
                "file": rel,
                "sha256": sha256_bytes(data),
                "labels": art["labels"],
                "trained_at": art["trained_at"],
                "train_first_open_time_ms": art["train_first_open_time_ms"],
                "train_last_open_time_ms": art["train_last_open_time_ms"],
                "n_obs": art["n_obs"],
                "convergence_rows": art["convergence"]["rows_needed"],
                "parity": art["parity"],
                "occupancy": art["occupancy"],
                "persistence": art["persistence"],
            }
    except ExportRefused as exc:
        print(f"EXPORT RIFIUTATO — {exc}")
        print("Il bot NON e' stato toccato.")
        return 1

    for m in MODULES:
        staged[BOT / m] = (HERE / m).read_bytes()      # copia VERBATIM, byte per byte

    meta = {
        "schema_version": META_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "decisions": "regime-training/DECISIONS.md",
        "n_states": N_STATES,
        "label_rule": regime_hmm.LABEL_RULE,
        "data_source": rs.SOURCE_ID,
        "granularity_seconds": rs.GRANULARITY_SECONDS,
        "feature_names": list(rf.FEATURE_NAMES),
        "feature_window_bars": rf.FEATURE_WINDOW_BARS,
        "fail_safe": {
            "min_feature_rows": MIN_FEATURE_ROWS,
            "min_contiguous_bars": MIN_CONTIGUOUS_BARS,
            "target_contiguous_bars": TARGET_CONTIGUOUS_BARS,
            "convergence_safety_factor": CONVERGENCE_SAFETY,
            "measured_on": f"modello a {N_STATES} stati (riverificato a ogni export)",
            "on_violation": "nessun regime -> nessun NUOVO trade nel ciclo; "
                            "STEP 0 (protezione posizioni) invariato",
        },
        "modules": module_sha,
        "models": models_meta,
    }
    staged[MODELS / "regime_model.meta.json"] = (
        json.dumps(meta, indent=2, sort_keys=True) + "\n").encode()

    # Fase 1: staging accanto alle destinazioni + verifica
    MODELS.mkdir(parents=True, exist_ok=True)
    tmps = {}
    for dest, data in staged.items():
        tmp = dest.with_name(dest.name + ".export-tmp")
        tmp.write_bytes(data)
        if sha256_file(tmp) != sha256_bytes(data):
            print(f"EXPORT RIFIUTATO — scrittura corrotta in staging: {tmp}")
            for t in tmps.values():
                t.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            return 1
        tmps[dest] = tmp
    # Fase 2: sostituzione
    for dest, tmp in tmps.items():
        os.replace(tmp, dest)
    # Fase 3: rilettura dal disco e verifica finale contro i metadati
    failures = []
    for m in MODULES:
        if sha256_file(BOT / m) != module_sha[m] or sha256_file(BOT / m) != sha256_file(HERE / m):
            failures.append(m)
    for asset, info in models_meta.items():
        if sha256_file(BOT / info["file"]) != info["sha256"]:
            failures.append(info["file"])
    if failures:
        print(f"ATTENZIONE — hash non coincidenti dopo la scrittura: {failures}. "
              f"Il detector rifiutera' di emettere un regime finche' non si riesporta.")
        return 1

    print("EXPORT COMPLETATO — verifiche superate, file riletti dal disco e ricontrollati.\n")
    for m in MODULES:
        print(f"  {m:22s} sha256 {module_sha[m][:16]}…  (copia verbatim)")
    for asset, info in models_meta.items():
        print(f"  {info['file']:22s} sha256 {info['sha256'][:16]}…  etichette {info['labels']} "
              f"· convergenza {info['convergence_rows']} righe")
    print(f"  models/regime_model.meta.json  fonte {rs.SOURCE_ID} · "
          f"{rs.GRANULARITY_SECONDS}s · min {MIN_FEATURE_ROWS} righe / "
          f"{MIN_CONTIGUOUS_BARS} barre · target {TARGET_CONTIGUOUS_BARS} barre")
    print("\nProssimo passo obbligato: COMMITTARE models/ e i tre moduli (il cloud riclona).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
