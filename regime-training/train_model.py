"""train_model.py — Passo 3: addestra un HMM gaussiano per asset (2 e 3 stati).

Per ogni asset legge data/features/{ASSET}_features.csv, standardizza le 4 feature
(z-score), addestra un GaussianHMM a covarianza diagonale con hmmlearn (piu'
restart, si tiene il miglior log-likelihood), ed esporta i parametri in JSON
(artifacts/), formato leggibile dallo scorer puro-Python regime_hmm.py.

Per ogni numero di stati (2 e 3) stampa i criteri di scelta concordati:
  BIC, log-likelihood, persistenza (diagonale della transizione), occupazione
  degli stati, centroidi in unita' originali (per etichettare range/trend).
La DECISIONE 2-vs-3 avviene nello step 4 (walk-forward + visivo); qui prepariamo
i numeri.

VERIFICA DI PARITA' (richiesta esplicita): dopo l'export, confronta l'output di
hmmlearn (score/predict/predict_proba) con quello di regime_hmm.py sugli STESSI
dati e stampa la differenza numerica. Se lo scorer puro non e' fedele, e' skew.

hmmlearn/numpy vivono SOLO qui (training). Il modello esportato e' JSON puro.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from hmmlearn import hmm

sys.path.insert(0, str(Path(__file__).parent))
import regime_features as rf   # noqa: E402  (per FEATURE_NAMES / FEATURE_WINDOW_BARS)
import regime_hmm              # noqa: E402  (lo scorer puro, per la parita')

FEATURES_DIR = Path(__file__).parent / "data" / "features"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ASSETS = ("BTC", "ETH", "SOL")
STATE_COUNTS = (2, 3)
N_RESTARTS = 10          # EM ha ottimi locali: piu' restart, si tiene il migliore
N_ITER = 300
RANDOM_SEED_BASE = 0


def _load_features(asset: str):
    """Ritorna (X, closes, times) dove X e' N x len(FEATURE_NAMES)."""
    path = FEATURES_DIR / f"{asset}_features.csv"
    X, closes, times = [], [], []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            X.append([float(r[name]) for name in rf.FEATURE_NAMES])
            closes.append(float(r["close"]))
            times.append(int(r["open_time_ms"]))
    return np.asarray(X, dtype=float), closes, times


def _fit_best(X_std: np.ndarray, n_states: int):
    """Addestra n_states, con restart multipli; ritorna il modello a LL massimo."""
    best_model, best_ll = None, -math.inf
    for seed in range(N_RESTARTS):
        model = hmm.GaussianHMM(
            n_components=n_states,
            covariance_type="diag",
            n_iter=N_ITER,
            tol=1e-4,
            random_state=RANDOM_SEED_BASE + seed,
        )
        try:
            model.fit(X_std)
            ll = model.score(X_std)
        except Exception:
            continue
        if math.isfinite(ll) and ll > best_ll:
            best_model, best_ll = model, ll
    if best_model is None:
        raise RuntimeError(f"nessun fit convergente per {n_states} stati")
    return best_model, best_ll


def _bic(loglik: float, n_states: int, n_features: int, n_obs: int) -> float:
    """BIC = -2*LL + k*ln(N). k = parametri liberi dell'HMM gaussiano diagonale."""
    k = (n_states - 1) + n_states * (n_states - 1) + 2 * n_states * n_features
    return -2.0 * loglik + k * math.log(n_obs)


def _diag_vars(model) -> np.ndarray:
    """Varianze diagonali (n_states x n_features), robusto alla forma di covars_."""
    return np.array([np.diag(c) for c in model.covars_])


def _to_model_dict(asset, model, scaler_mean, scaler_std, labels) -> dict:
    means = model.means_
    vars_ = _diag_vars(model)
    return {
        "asset": asset,
        "n_states": int(model.n_components),
        "n_features": len(rf.FEATURE_NAMES),
        "feature_names": list(rf.FEATURE_NAMES),
        "startprob": model.startprob_.tolist(),
        "transmat": model.transmat_.tolist(),
        "means": means.tolist(),
        "vars": vars_.tolist(),
        "scaler_mean": scaler_mean.tolist(),
        "scaler_std": scaler_std.tolist(),
        "labels": labels,
    }


def _label_states(centroids_orig: list[dict]) -> dict:
    """Etichettatura provvisoria per stato, ordinando per ADX crescente.

    2 stati: piu' basso ADX -> 'range', piu' alto -> 'trend'.
    3 stati: basso -> 'range', alto -> 'trend', intermedio -> 'transition'.
    E' provvisoria (display): la mappa finale si fissa allo step 5 dopo la
    conferma visiva. Serve solo a rendere leggibile il report.
    """
    order = sorted(range(len(centroids_orig)), key=lambda s: centroids_orig[s]["adx"])
    labels = {}
    if len(order) == 2:
        labels[str(order[0])] = "range"
        labels[str(order[1])] = "trend"
    else:
        labels[str(order[0])] = "range"
        labels[str(order[-1])] = "trend"
        for mid in order[1:-1]:
            labels[str(mid)] = "transition"
    return labels


def _parity_check(model, model_dict, X_std, X_raw) -> dict:
    """Confronta hmmlearn vs regime_hmm (puro Python) sugli stessi dati."""
    # hmmlearn lavora sui dati gia' standardizzati (come nel fit);
    # regime_hmm riceve i dati grezzi e ri-standardizza col medesimo scaler.
    ll_hmm = float(model.score(X_std))
    proba_hmm = model.predict_proba(X_std)
    states_hmm = model.predict(X_std)

    ll_pp = regime_hmm.score(model_dict, X_raw)
    proba_pp = np.asarray(regime_hmm.predict_proba(model_dict, X_raw))
    states_pp = np.asarray(regime_hmm.predict(model_dict, X_raw))

    return {
        "loglik_abs_diff": abs(ll_hmm - ll_pp),
        "proba_max_abs_diff": float(np.max(np.abs(proba_hmm - proba_pp))),
        "viterbi_mismatches": int(np.sum(states_hmm != states_pp)),
        "n_obs": len(X_raw),
    }


def train_asset(asset: str) -> int:
    X, closes, times = _load_features(asset)
    n_obs, n_features = X.shape
    scaler_mean = X.mean(axis=0)
    scaler_std = X.std(axis=0, ddof=0)
    X_std = (X - scaler_mean) / scaler_std
    X_raw = X.tolist()

    print(f"\n=== {asset} · {n_obs} osservazioni · feature {list(rf.FEATURE_NAMES)} ===")
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    for n_states in STATE_COUNTS:
        model, loglik = _fit_best(X_std, n_states)
        bic = _bic(loglik, n_states, n_features, n_obs)

        # Centroidi in unita' ORIGINALI (de-standardizzati) per etichettare.
        centroids_orig = []
        for s in range(n_states):
            vals = model.means_[s] * scaler_std + scaler_mean
            centroids_orig.append(dict(zip(rf.FEATURE_NAMES, vals.tolist())))
        labels = _label_states(centroids_orig)

        # Occupazione e persistenza.
        states = model.predict(X_std)
        occupancy = [float(np.mean(states == s)) for s in range(n_states)]
        persistence = [float(model.transmat_[s][s]) for s in range(n_states)]

        model_dict = _to_model_dict(asset, model, scaler_mean, scaler_std, labels)
        out = ARTIFACTS_DIR / f"{asset}_hmm_{n_states}.json"
        payload = {
            **model_dict,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "n_obs": n_obs,
            "loglik": loglik,
            "bic": bic,
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        parity = _parity_check(model, model_dict, X_std, X_raw)

        # ── Report ────────────────────────────────────────────────────────────
        print(f"\n  [{n_states} stati]  logL={loglik:,.1f}   BIC={bic:,.1f}")
        for s in range(n_states):
            c = centroids_orig[s]
            print(
                f"    stato {s} '{labels[str(s)]:10s}' "
                f"occ={occupancy[s]*100:5.1f}%  persist={persistence[s]:.3f}  |  "
                f"ADX={c['adx']:5.1f}  ATR%={c['atr_pct']*100:4.2f}  "
                f"KER={c['kaufman_er']:.3f}  vol={c['volume_ratio']:.2f}"
            )
        print(
            f"    PARITA' scorer-puro vs hmmlearn: "
            f"logL Δ={parity['loglik_abs_diff']:.2e}  "
            f"proba maxΔ={parity['proba_max_abs_diff']:.2e}  "
            f"Viterbi diff={parity['viterbi_mismatches']}/{parity['n_obs']}"
        )
        print(f"    -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Addestra HMM di regime per asset")
    parser.add_argument("--assets", nargs="+", default=list(ASSETS), choices=list(ASSETS))
    args = parser.parse_args(argv)
    for asset in args.assets:
        train_asset(asset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
