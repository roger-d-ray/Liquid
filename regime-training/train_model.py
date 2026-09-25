"""train_model.py — Passo 3: addestra l'HMM di regime (2 stati) per asset.

Il numero di stati NON e' un parametro: e' la decisione chiusa di DECISIONS.md §1.
Il retraining ricalcola i parametri, non rimette in discussione l'architettura.
(validate_model.py resta lo strumento di ricerca che confronta 2 e 3 stati.)

Per ogni asset produce artifacts/{ASSET}_hmm_2.json con dentro, oltre ai
parametri, tutto cio' che serve a dimostrare la coerenza train/serve:

  features_module_sha256  hash di regime_features.py CON CUI E' STATO COSTRUITO
                          il dataset (dal manifest di build_dataset, non ricalcolato)
  scorer_module_sha256    hash di regime_hmm.py con cui e' stata verificata la parita'
  provenance              fonte, granularita' e hash di regime_source.py al fetch
  parity                  scorer puro vs hmmlearn — BLOCCANTE se non fedele
  convergence             righe di feature necessarie per QUESTO modello — cosi'
                          la soglia minima del fail-safe viene riverificata a ogni
                          retraining invece di essere data per scontata
  labels / label_rule     etichette derivate dai centroidi (regime_hmm.derive_labels)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from hmmlearn import hmm

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402

FEATURES_DIR = HERE / "data" / "features"
ARTIFACTS_DIR = HERE / "artifacts"
ASSETS = ("BTC", "ETH", "SOL")

N_STATES = 2          # DECISIONS.md §1 — non modificare nel retraining
N_RESTARTS = 10
N_ITER = 300

# Soglie di parita' scorer puro vs hmmlearn (osservato: ~1e-8 / 1e-11 / 0)
PARITY_MAX_PROBA_DIFF = 1e-6
PARITY_MAX_LOGLIK_DIFF = 1e-6

# Misura di convergenza della posteriori filtrata (DECISIONS.md §4)
CONVERGENCE_GRID = (5, 10, 15, 20, 30, 40, 50, 75, 100)
CONVERGENCE_TOL = 1e-4
CONVERGENCE_FIRST_ENDPOINT = 3000
CONVERGENCE_STEP = 900


class TrainingRefused(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_features(asset):
    X, times = [], []
    with (FEATURES_DIR / f"{asset}_features.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            X.append([float(r[n]) for n in rf.FEATURE_NAMES])
            times.append(int(r["open_time_ms"]))
    return np.asarray(X, float), times


def fit_best(X_std):
    best, best_ll = None, -math.inf
    for seed in range(N_RESTARTS):
        m = hmm.GaussianHMM(n_components=N_STATES, covariance_type="diag",
                            n_iter=N_ITER, tol=1e-4, random_state=seed)
        try:
            m.fit(X_std)
            ll = m.score(X_std)
        except Exception:
            continue
        if math.isfinite(ll) and ll > best_ll:
            best, best_ll = m, ll
    if best is None:
        raise TrainingRefused("nessun fit convergente")
    return best, best_ll


def measure_convergence(model: dict, X: list) -> dict:
    """Minimo di righe di feature per cui la decisione filtrata all'ultima barra
    coincide con quella a storia piena (accordo 100%, |Δconfidence| <= TOL)."""
    endpoints = list(range(CONVERGENCE_FIRST_ENDPOINT, len(X), CONVERGENCE_STEP))
    refs = [regime_hmm.predict_regime(model, X[:e]) for e in endpoints]
    table, rows_needed = {}, None
    for L in CONVERGENCE_GRID:
        agree, err = 0, 0.0
        for ref, e in zip(refs, endpoints):
            part = regime_hmm.predict_regime(model, X[e - L:e])
            agree += part["state"] == ref["state"]
            err = max(err, abs(part["confidence"] - ref["confidence"]))
        table[str(L)] = {"state_agreement": agree / len(endpoints), "max_conf_err": err}
        if rows_needed is None and agree == len(endpoints) and err <= CONVERGENCE_TOL:
            rows_needed = L
    return {"rows_needed": rows_needed, "tolerance": CONVERGENCE_TOL,
            "endpoints": len(endpoints), "reference": "storia piena", "grid": table}


def train_asset(asset: str, manifest: dict) -> dict:
    # 1) Il dataset e' stato costruito con il codice feature attuale?
    entry = manifest.get(asset)
    if entry is None:
        raise TrainingRefused("manifest assente: rilancia build_dataset.py")
    current = sha256_file(HERE / "regime_features.py")
    if entry["features_module_sha256"] != current:
        raise TrainingRefused(
            "dataset costruito con un regime_features.py DIVERSO da quello attuale "
            "(skew train/serve): rilancia build_dataset.py")
    if entry["feature_names"] != list(rf.FEATURE_NAMES):
        raise TrainingRefused("ordine/insieme delle feature cambiato: rilancia build_dataset.py")

    X, times = load_features(asset)
    mu, sd = X.mean(axis=0), X.std(axis=0, ddof=0)
    if not np.all(sd > 0):
        raise TrainingRefused("una feature ha varianza nulla")
    X_std = (X - mu) / sd
    X_list = X.tolist()

    model, loglik = fit_best(X_std)
    n, d = X.shape
    k = (N_STATES - 1) + N_STATES * (N_STATES - 1) + 2 * N_STATES * d
    params = {
        "asset": asset,
        "n_states": N_STATES,
        "n_features": d,
        "feature_names": list(rf.FEATURE_NAMES),
        "startprob": model.startprob_.tolist(),
        "transmat": model.transmat_.tolist(),
        "means": model.means_.tolist(),
        "vars": np.array([np.diag(c) for c in model.covars_]).tolist(),
        "scaler_mean": mu.tolist(),
        "scaler_std": sd.tolist(),
    }

    # 2) Etichette derivate dai centroidi (solleva se ADX/KER non concordano)
    params["labels"] = regime_hmm.derive_labels(params)
    params["label_rule"] = regime_hmm.LABEL_RULE

    # 3) Parita' scorer puro vs hmmlearn — BLOCCANTE
    scorer_sha = sha256_file(HERE / "regime_hmm.py")
    proba_pp = np.asarray(regime_hmm.predict_proba(params, X_list))
    parity = {
        "loglik_abs_diff": abs(float(model.score(X_std)) - regime_hmm.score(params, X_list)),
        "proba_max_abs_diff": float(np.max(np.abs(model.predict_proba(X_std) - proba_pp))),
        "viterbi_mismatches": int(np.sum(model.predict(X_std)
                                         != np.asarray(regime_hmm.predict(params, X_list)))),
        "n_obs": n,
    }
    if (parity["viterbi_mismatches"] or parity["proba_max_abs_diff"] > PARITY_MAX_PROBA_DIFF
            or parity["loglik_abs_diff"] > PARITY_MAX_LOGLIK_DIFF):
        raise TrainingRefused(f"scorer puro NON fedele a hmmlearn: {parity}")

    # 4) Convergenza del filtraggio su QUESTO modello
    convergence = measure_convergence(params, X_list)

    occupancy = np.bincount(model.predict(X_std), minlength=N_STATES) / n
    return {
        **params,
        "features_module_sha256": entry["features_module_sha256"],
        "scorer_module_sha256": scorer_sha,
        "provenance": entry["history_provenance"],
        "feature_window_bars": entry["feature_window_bars"],
        "parity": parity,
        "convergence": convergence,
        "occupancy": {params["labels"][str(s)]: float(occupancy[s]) for s in range(N_STATES)},
        "persistence": {params["labels"][str(s)]: float(model.transmat_[s][s])
                        for s in range(N_STATES)},
        "train_first_open_time_ms": times[0],
        "train_last_open_time_ms": times[-1],
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_obs": n,
        "loglik": loglik,
        "bic": -2 * loglik + k * math.log(n),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Addestra l'HMM di regime (2 stati) per asset")
    p.add_argument("--assets", nargs="+", default=list(ASSETS), choices=list(ASSETS))
    args = p.parse_args(argv)
    manifest_path = FEATURES_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    code = 0
    for asset in args.assets:
        try:
            art = train_asset(asset, manifest)
        except (TrainingRefused, regime_hmm.LabelError) as exc:
            print(f"[{asset}] RIFIUTATO: {exc}")
            code = 1
            continue
        out = ARTIFACTS_DIR / f"{asset}_hmm_{N_STATES}.json"
        out.write_text(json.dumps(art, indent=2), encoding="utf-8")
        par, conv = art["parity"], art["convergence"]
        print(f"[{asset}] {art['n_obs']} obs · logL {art['loglik']:,.1f} · "
              f"etichette {art['labels']}")
        print(f"        occupazione {art['occupancy']} · persistenza "
              f"{ {k: round(v, 3) for k, v in art['persistence'].items()} }")
        print(f"        parita': logL Δ {par['loglik_abs_diff']:.1e} · proba maxΔ "
              f"{par['proba_max_abs_diff']:.1e} · Viterbi diff {par['viterbi_mismatches']}")
        print(f"        convergenza filtraggio: {conv['rows_needed']} righe "
              f"(tol {conv['tolerance']}, {conv['endpoints']} punti) -> {out.name}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
