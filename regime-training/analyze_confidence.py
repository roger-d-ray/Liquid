"""analyze_confidence.py — la soglia di confidence regge fuori campione?

Serve a decidere (e a riverificare a ogni retraining) la soglia di confidence
sotto la quale il detector NON dichiara un regime operativo. Tre domande:

  1. Dove sta il "ginocchio", cioe' la confidence oltre la quale l'errore crolla?
  2. Il ginocchio resta li' anche FUORI CAMPIONE (walk-forward), o era un
     artefatto del modello valutato sui propri dati di training?
  3. La soglia lavora in modo simmetrico su "range" e "trend"?
  4. Le ore escluse si concentrano davvero sulle transizioni di regime?

"Errore" = lo stato FILTRATO online (cio' che il bot vede) differisce da quello
Viterbi retrospettivo (cio' che col senno di poi era piu' probabile). NON e' PnL:
misura quanto spesso l'etichetta viene rivista, non quanto si guadagna.

Criterio del ginocchio, fissato PRIMA di vedere i numeri: la soglia minima t
(griglia 0,01) tale che OGNI fascia locale [u, u+0,02) con u >= t abbia errore
<= 5%. Riportato anche con 3% e 10%, per mostrare che non dipende dalla scelta.

Fedelta' al live:
- il filtro gira su sequenze CONTIGUE (spezzate ai buchi della fonte) e una riga
  conta solo se il filtro ha gia' visto >= 50 righe contigue: come il detector;
- in walk-forward il modello e lo scaler vedono SOLO il passato del fold; Viterbi
  (riferimento retrospettivo) usa anche dati successivi al fold, perche' e' il
  "senno di poi" e non entra nei parametri: senza questo, a fine fold il
  riferimento coinciderebbe col filtro e l'errore sarebbe artificialmente basso.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
from hmmlearn import hmm

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402

FEATURES_DIR = HERE / "data" / "features"
MODELS_DIR = HERE.parent / "models"
REPORT = HERE / "artifacts" / "reports" / "confidence_analysis.json"
ASSETS = ("BTC", "ETH", "SOL")
HOUR_MS = 3600 * 1000

MIN_FEATURE_ROWS = 50       # come il fail-safe del detector
N_FOLDS, TEST_BLOCK = 8, 1300   # stesso protocollo dello step 4
CONTEXT = 200               # righe di train prima del fold, per il filtro online
FUTURE = 200                # righe dopo il fold, SOLO per il riferimento Viterbi
WF_RESTARTS, WF_ITER = 5, 300

KNEE_GRID = [round(0.80 + 0.01 * i, 2) for i in range(18)]      # 0,80 .. 0,97
KNEE_BAND = 0.02
KNEE_CRITERIA = (0.03, 0.05, 0.10)
PRICE_THRESHOLDS = (0.90, 0.93, 0.95, 0.97, 0.99)
CANDIDATE = 0.95
BANDS = [(0.50, 0.80), (0.80, 0.90), (0.90, 0.93), (0.93, 0.95),
         (0.95, 0.97), (0.97, 0.99), (0.99, 1.01)]
NEAR_SWITCH_HOURS = 6


def load(asset):
    X, t = [], []
    with (FEATURES_DIR / f"{asset}_features.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            X.append([float(r[n]) for n in rf.FEATURE_NAMES])
            t.append(int(r["open_time_ms"]))
    return np.asarray(X, float), np.asarray(t)


def segments(times):
    """Intervalli [a, b) di righe contigue nel tempo (spezzati ai buchi)."""
    cuts = [0] + [i for i in range(1, len(times)) if times[i] - times[i - 1] != HOUR_MS]
    return list(zip(cuts, cuts[1:] + [len(times)]))


def evaluate(model, X, times, lo, hi):
    """Righe [lo, hi) valutate come le vedrebbe il bot live.

    Ritorna array (confidence, stato_online, stato_retro, distanza_da_switch).
    """
    conf, on, retro, dist = [], [], [], []
    for a, b in segments(times):
        f0, f1 = max(a, lo - CONTEXT), min(b, hi)           # finestra del filtro
        v1 = min(b, hi + FUTURE)                             # finestra del riferimento
        if f1 - f0 < MIN_FEATURE_ROWS:
            continue
        rows_f = X[f0:f1].tolist()
        post = np.asarray(regime_hmm.filtered_posteriors(model, rows_f))
        vit = np.asarray(regime_hmm.predict(model, X[f0:v1].tolist()))
        switches = np.flatnonzero(vit[1:] != vit[:-1]) + 1  # indici relativi a f0
        for j in range(max(lo, f0), f1):
            k = j - f0
            if k + 1 < MIN_FEATURE_ROWS:                   # il detector tacerebbe
                continue
            conf.append(post[k].max())
            on.append(int(post[k].argmax()))
            retro.append(int(vit[k]))
            dist.append(int(np.min(np.abs(switches - k))) if len(switches) else 10**6)
    return np.asarray(conf), np.asarray(on), np.asarray(retro), np.asarray(dist)


def fit_fold(X_train):
    mu, sd = X_train.mean(axis=0), X_train.std(axis=0, ddof=0)
    Xs = (X_train - mu) / sd
    best, best_ll = None, -math.inf
    for seed in range(WF_RESTARTS):
        m = hmm.GaussianHMM(n_components=2, covariance_type="diag",
                            n_iter=WF_ITER, tol=1e-4, random_state=seed)
        try:
            m.fit(Xs)
            ll = m.score(Xs)
        except Exception:
            continue
        if math.isfinite(ll) and ll > best_ll:
            best, best_ll = m, ll
    model = {"n_states": 2, "feature_names": list(rf.FEATURE_NAMES),
             "startprob": best.startprob_.tolist(), "transmat": best.transmat_.tolist(),
             "means": best.means_.tolist(),
             "vars": np.array([np.diag(c) for c in best.covars_]).tolist(),
             "scaler_mean": mu.tolist(), "scaler_std": sd.tolist()}
    model["labels"] = regime_hmm.derive_labels(model)
    return model


def knee(conf, wrong, criterion):
    """Soglia minima t oltre la quale ogni fascia [u, u+0,02) ha errore <= criterio."""
    band_err = {}
    for u in KNEE_GRID:
        sel = (conf >= u) & (conf < u + KNEE_BAND)
        band_err[u] = wrong[sel].mean() if sel.sum() >= 30 else None
    for t in KNEE_GRID:
        tail = [band_err[u] for u in KNEE_GRID if u >= t]
        if all(e is not None and e <= criterion for e in tail):
            return t
    return None


def summarise(name, conf, on, retro, dist, labels):
    wrong = on != retro
    out = {"hours": int(len(conf)), "base_error": float(wrong.mean()),
           "median_conf": float(np.median(conf)),
           "knee": {str(c): knee(conf, wrong, c) for c in KNEE_CRITERIA},
           "bands": [], "price": [], "per_state": {}, "transitions": {}}
    for lo, hi in BANDS:
        sel = (conf >= lo) & (conf < hi)
        if sel.any():
            out["bands"].append({"band": [lo, min(hi, 1.0)], "share": float(sel.mean()),
                                 "error": float(wrong[sel].mean())})
    for thr in PRICE_THRESHOLDS:
        keep = conf >= thr
        out["price"].append({"threshold": thr, "excluded": float((~keep).mean()),
                             "error_kept": float(wrong[keep].mean()) if keep.any() else None})
    # Dettaglio per stato, sullo stato che il bot VEDE (online)
    for s in (0, 1):
        lab = labels[str(s)]
        sel = on == s
        if not sel.any():
            continue
        keep = sel & (conf >= CANDIDATE)
        out["per_state"][lab] = {
            "share_of_hours": float(sel.mean()),
            "excluded_within_state": float((sel & (conf < CANDIDATE)).sum() / sel.sum()),
            "error_no_threshold": float(wrong[sel].mean()),
            "error_with_threshold": float(wrong[keep].mean()) if keep.any() else None,
        }
    # Le ore escluse stanno vicino alle transizioni? Confronto col caso base.
    excl = conf < CANDIDATE
    near = dist <= NEAR_SWITCH_HOURS
    out["transitions"] = {
        "window_hours": NEAR_SWITCH_HOURS,
        "near_share_all_hours": float(near.mean()),
        "near_share_excluded": float(near[excl].mean()) if excl.any() else None,
        "excluded_share_among_near": float(excl[near].mean()) if near.any() else None,
        "excluded_share_among_far": float(excl[~near].mean()) if (~near).any() else None,
    }
    return out


def run_asset(asset):
    X, times = load(asset)
    n = len(X)
    final = json.loads((MODELS_DIR / f"regime_{asset}.json").read_text())
    ins = evaluate(final, X, times, 0, n)
    res_in = summarise("in-sample", *ins, final["labels"])

    # Walk-forward: le etichette di ogni fold vengono dalla regola sui centroidi,
    # quindi "range"/"trend" hanno lo stesso significato in tutti i fold.
    init = n - N_FOLDS * TEST_BLOCK
    parts = {k: [] for k in ("conf", "on_lab", "retro_lab", "dist")}
    for f in range(N_FOLDS):
        lo = init + f * TEST_BLOCK
        model = fit_fold(X[:lo])
        c, o, r, d = evaluate(model, X, times, lo, lo + TEST_BLOCK)
        trend = int([k for k, v in model["labels"].items() if v == "trend"][0])
        parts["conf"].append(c)
        parts["on_lab"].append((o == trend).astype(int))        # 1 = trend, 0 = range
        parts["retro_lab"].append((r == trend).astype(int))
        parts["dist"].append(d)
    cat = {k: np.concatenate(v) for k, v in parts.items()}
    res_wf = summarise("walk-forward", cat["conf"], cat["on_lab"], cat["retro_lab"],
                       cat["dist"], {"0": "range", "1": "trend"})
    return {"asset": asset, "in_sample": res_in, "walk_forward": res_wf}


def pct(x, d=2):
    return "  —  " if x is None else f"{x*100:.{d}f}%"


def show(r):
    a, i, w = r["asset"], r["in_sample"], r["walk_forward"]
    print(f"\n{'='*78}\n{a} · in-sample {i['hours']} ore · walk-forward {w['hours']} ore held-out\n{'='*78}")
    print(f"  mediana confidence        in-sample {i['median_conf']:.4f} · WF {w['median_conf']:.4f}")
    print(f"  errore senza soglia       in-sample {pct(i['base_error'])} · WF {pct(w['base_error'])}")
    print("\n  GINOCCHIO (fascia 0,02 con errore <= criterio, per ogni u >= t)")
    for c in KNEE_CRITERIA:
        print(f"    criterio {c*100:>4.0f}%   in-sample {i['knee'][str(c)]}   ·   "
              f"walk-forward {w['knee'][str(c)]}")
    print("\n  Errore per fascia            in-sample (% ore / errore)   walk-forward (% ore / errore)")
    wb = {tuple(b["band"]): b for b in w["bands"]}
    for b in i["bands"]:
        o = wb.get(tuple(b["band"]))
        lo, hi = b["band"]
        print(f"    [{lo:.2f}, {hi:.2f})   {pct(b['share'],1):>7s} / {pct(b['error']):>7s}"
              f"          {pct(o['share'],1) if o else '—':>7s} / {pct(o['error']) if o else '—':>7s}")
    print("\n  Prezzo della soglia          in-sample (escluse → errore)   walk-forward")
    for pi, pw in zip(i["price"], w["price"]):
        print(f"    {pi['threshold']:.2f}                  {pct(pi['excluded'],1):>6s} → "
              f"{pct(pi['error_kept']):>6s}          {pct(pw['excluded'],1):>6s} → {pct(pw['error_kept']):>6s}")
    print(f"\n  Per stato, soglia {CANDIDATE}   (walk-forward; in-sample fra parentesi)")
    for lab in ("range", "trend"):
        s, si = w["per_state"].get(lab), i["per_state"].get(lab)
        if s:
            print(f"    {lab:6s} {pct(s['share_of_hours'],1)} delle ore · escluse {pct(s['excluded_within_state'],1)} "
                  f"({pct(si['excluded_within_state'],1)}) · errore {pct(s['error_no_threshold'])} → "
                  f"{pct(s['error_with_threshold'])}  ({pct(si['error_no_threshold'])} → {pct(si['error_with_threshold'])})")
    t, ti = w["transitions"], i["transitions"]
    print(f"\n  Le ore escluse stanno sulle transizioni? (entro ±{t['window_hours']}h da un cambio Viterbi)")
    print(f"    ore vicine a una transizione: {pct(t['near_share_all_hours'],1)} di tutte, "
          f"ma {pct(t['near_share_excluded'],1)} delle escluse  (in-sample {pct(ti['near_share_all_hours'],1)} → {pct(ti['near_share_excluded'],1)})")
    print(f"    probabilita' di esclusione: vicino a una transizione {pct(t['excluded_share_among_near'],1)} · "
          f"lontano {pct(t['excluded_share_among_far'],1)}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Soglia di confidence: in-sample vs walk-forward")
    p.add_argument("--assets", nargs="+", default=list(ASSETS), choices=list(ASSETS))
    args = p.parse_args(argv)
    results = []
    for a in args.assets:
        r = run_asset(a)
        show(r)
        results.append(r)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n-> {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
