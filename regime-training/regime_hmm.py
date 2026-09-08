"""regime_hmm.py — scorer Gaussian-HMM in Python PURO (zero dipendenze).

Questo file applica un HMM gaussiano a covarianza diagonale gia' addestrato, i
cui parametri arrivano da un JSON (prodotto da train_model.py). Serve a UN solo
scopo: far girare il modello nel bot live SENZA numpy/scipy/scikit-learn/hmmlearn,
coerente con la filosofia "puro Python" del progetto.

E' l'analogo, per il MODELLO, di regime_features.py per le FEATURE: sorgente
unica, copiata verbatim nel bot ed hashata nei metadati. La sua fedelta' rispetto
al modello hmmlearn originale e' verificata da un test di parita' in
train_model.py (come abbiamo fatto per ADX/ATR): se lo scorer non riproduce le
probabilita'/stati di hmmlearn, saremmo di nuovo in training-serving skew, ma sul
modello.

Formato del modello (dict / JSON)
----------------------------------
{
  "n_states": int, "n_features": int, "feature_names": [...],
  "startprob":  [S],            # prob. iniziali di stato
  "transmat":   [S][S],         # matrice di transizione
  "means":      [S][D],         # medie per stato, nello spazio STANDARDIZZATO
  "vars":       [S][D],         # varianze (diagonale), spazio standardizzato
  "scaler_mean":[D], "scaler_std":[D],   # z-score applicato prima dell'HMM
  "labels":     {"0": "range", ...}      # mappa stato->etichetta (opz.)
}

Le funzioni chiave replicano l'API di hmmlearn che ci serve:
- score()         ~ GaussianHMM.score()        (log-likelihood, forward)
- predict()       ~ GaussianHMM.predict()       (Viterbi, sequenza di stati)
- predict_proba() ~ GaussianHMM.predict_proba() (posteriori forward-backward)
- predict_regime()  = risposta operativa per l'ULTIMA barra: {state, label, confidence}
"""

from __future__ import annotations

import json
import math
from pathlib import Path

_LOG_2PI = math.log(2.0 * math.pi)
_NEG_INF = float("-inf")


# ── Caricamento ───────────────────────────────────────────────────────────────
def load_model(source) -> dict:
    """Accetta un path (str/Path) a un JSON o un dict gia' pronto."""
    if isinstance(source, (str, Path)):
        source = json.loads(Path(source).read_text(encoding="utf-8"))
    return source


# ── Utilita' numeriche in log-space ──────────────────────────────────────────
def _logsumexp(values: list[float]) -> float:
    m = max(values)
    if m == _NEG_INF:
        return _NEG_INF
    return m + math.log(sum(math.exp(v - m) for v in values))


def _standardize(row: list[float], mean: list[float], std: list[float]) -> list[float]:
    """Applica lo stesso z-score usato in training (scaler congelato nel JSON)."""
    return [(x - m) / s for x, m, s in zip(row, mean, std)]


def _log_gaussian_diag(x: list[float], mean: list[float], var: list[float]) -> float:
    """log N(x; mean, diag(var)) per una singola osservazione e un singolo stato."""
    total = 0.0
    for xd, md, vd in zip(x, mean, var):
        total += _LOG_2PI + math.log(vd) + (xd - md) ** 2 / vd
    return -0.5 * total


def _emission_logprobs(X_std: list[list[float]], model: dict) -> list[list[float]]:
    """Matrice T x S dei log-emission: logB[t][s] = log P(obs_t | stato s)."""
    means, vars = model["means"], model["vars"]
    return [
        [_log_gaussian_diag(x, means[s], vars[s]) for s in range(model["n_states"])]
        for x in X_std
    ]


def _prep(model: dict, X_raw: list[list[float]]):
    """Standardizza gli input e prepara logstart, logtrans, logB."""
    mean, std = model["scaler_mean"], model["scaler_std"]
    X_std = [_standardize(row, mean, std) for row in X_raw]
    log_start = [math.log(p) if p > 0 else _NEG_INF for p in model["startprob"]]
    log_trans = [
        [math.log(p) if p > 0 else _NEG_INF for p in row] for row in model["transmat"]
    ]
    log_b = _emission_logprobs(X_std, model)
    return log_start, log_trans, log_b


# ── Forward-backward (posteriori) e forward (log-likelihood) ─────────────────
def _forward(log_start, log_trans, log_b):
    """Ritorna (log_alpha, loglik). log_alpha[t][j] = log P(obs_1..t, stato_t=j)."""
    T, S = len(log_b), len(log_start)
    log_alpha = [[_NEG_INF] * S for _ in range(T)]
    for j in range(S):
        log_alpha[0][j] = log_start[j] + log_b[0][j]
    for t in range(1, T):
        for j in range(S):
            prev = [log_alpha[t - 1][i] + log_trans[i][j] for i in range(S)]
            log_alpha[t][j] = _logsumexp(prev) + log_b[t][j]
    return log_alpha, _logsumexp(log_alpha[-1])


def _backward(log_trans, log_b):
    """log_beta[t][i] = log P(obs_{t+1..T} | stato_t=i)."""
    T, S = len(log_b), len(log_trans)
    log_beta = [[_NEG_INF] * S for _ in range(T)]
    for i in range(S):
        log_beta[T - 1][i] = 0.0
    for t in range(T - 2, -1, -1):
        for i in range(S):
            nxt = [
                log_trans[i][j] + log_b[t + 1][j] + log_beta[t + 1][j]
                for j in range(S)
            ]
            log_beta[t][i] = _logsumexp(nxt)
    return log_beta


def score(model: dict, X_raw: list[list[float]]) -> float:
    """Log-likelihood totale della sequenza (come GaussianHMM.score)."""
    log_start, log_trans, log_b = _prep(model, X_raw)
    _, loglik = _forward(log_start, log_trans, log_b)
    return loglik


def predict_proba(model: dict, X_raw: list[list[float]]) -> list[list[float]]:
    """Posteriori gamma[t][s] = P(stato_t=s | tutte le osservazioni)."""
    log_start, log_trans, log_b = _prep(model, X_raw)
    log_alpha, loglik = _forward(log_start, log_trans, log_b)
    log_beta = _backward(log_trans, log_b)
    T, S = len(log_b), len(log_start)
    gamma = []
    for t in range(T):
        row = [log_alpha[t][s] + log_beta[t][s] - loglik for s in range(S)]
        gamma.append([math.exp(v) for v in row])
    return gamma


def predict(model: dict, X_raw: list[list[float]]) -> list[int]:
    """Sequenza di stati piu' probabile (Viterbi), come GaussianHMM.predict."""
    log_start, log_trans, log_b = _prep(model, X_raw)
    T, S = len(log_b), len(log_start)
    delta = [[_NEG_INF] * S for _ in range(T)]
    back = [[0] * S for _ in range(T)]
    for j in range(S):
        delta[0][j] = log_start[j] + log_b[0][j]
    for t in range(1, T):
        for j in range(S):
            best_i, best_val = 0, _NEG_INF
            for i in range(S):
                val = delta[t - 1][i] + log_trans[i][j]
                if val > best_val:
                    best_val, best_i = val, i
            delta[t][j] = best_val + log_b[t][j]
            back[t][j] = best_i
    # backtrack
    last = max(range(S), key=lambda s: delta[T - 1][s])
    states = [last]
    for t in range(T - 1, 0, -1):
        last = back[t][last]
        states.append(last)
    states.reverse()
    return states


# ── Risposta operativa per il bot live: regime dell'ULTIMA barra ─────────────
def predict_regime(model: dict, X_raw: list[list[float]]) -> dict:
    """Regime filtrato all'ultimo istante: {state, label, confidence, probs}.

    La 'confidence' e' la probabilita' a posteriori dello stato piu' probabile
    all'ultima barra. Usiamo il forward (posteriore filtrato P(stato_T|obs_1..T)):
    all'ultimo istante coincide con lo smoothed, e non richiede dati futuri —
    corretto per l'uso online in produzione.
    """
    log_start, log_trans, log_b = _prep(model, X_raw)
    log_alpha, loglik = _forward(log_start, log_trans, log_b)
    S = len(log_start)
    last = log_alpha[-1]
    denom = _logsumexp(last)
    probs = [math.exp(last[s] - denom) for s in range(S)]
    best = max(range(S), key=lambda s: probs[s])
    labels = model.get("labels") or {}
    return {
        "state": best,
        "label": labels.get(str(best), labels.get(best)),
        "confidence": probs[best],
        "probs": probs,
        "loglik": loglik,
    }
