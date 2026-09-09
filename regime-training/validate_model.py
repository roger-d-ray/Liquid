"""validate_model.py — Passo 4: validazione e decisione 2-vs-3 stati.

Criteri PRE-REGISTRATI (fissati prima di vedere i numeri), in ordine di priorita':
  1. Usabilita' in live (sezione D)  — VETO
  2. Mappatura operativa sulle skill — VETO (decisione di trading, fuori da questo script)
  3. Delta-LL held-out sopra soglia (sezione B)
  4. Test strutturali (persistenza, occupazione, interpretabilita')
  5. BIC in-sample — indicativo, non decisivo
In caso di pareggio o ambiguita', vincono 2 stati.

Sezioni prodotte:
  A. Anatomia del terzo stato: centroidi standardizzati feature per feature,
     identificazione dello stato "nuovo" (quello che 2 stati non aveva) e quale
     feature domina la sua separazione.
  A2. Controprova: riaddestramento con sole ADX+KER. Se il vantaggio del terzo
     stato crolla, il terzo stato era volatilita' travestita.
  B. Walk-forward expanding: Delta-LL per barra held-out (3 stati - 2 stati), per
     fold, con dispersione e consistenza di segno. Scaler rifittato DENTRO ogni
     fold sul solo train (nessun leakage).
  C. Struttura: persistenza, occupazione.
  D. Usabilita' live: disaccordo filtrato-online vs Viterbi-retrospettivo, churn
     del segnale online, durata degli episodi, sensibilita' alla lunghezza di
     sequenza.
  E. Grafici: prezzo con sfondo per regime, ONLINE (cio' che il bot vedrebbe)
     confrontato con Viterbi (retrospettivo).

hmmlearn/numpy/matplotlib restano confinati qui (training). La matematica online
usata per le metriche live e' quella di regime_hmm.py, cioe' esattamente il codice
che girera' nel bot.
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

sys.path.insert(0, str(Path(__file__).parent))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402

FEATURES_DIR = Path(__file__).parent / "data" / "features"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
REPORTS_DIR = ARTIFACTS_DIR / "reports"
ASSETS = ("BTC", "ETH", "SOL")

# ── Protocollo walk-forward, dichiarato prima dell'esecuzione ────────────────
N_FOLDS = 8
TEST_BLOCK = 1300           # ~54 giorni di barre 1h per fold
WF_RESTARTS = 3             # restart EM dentro ogni fold (selezione sul TRAIN)
WF_ITER = 200
FULL_RESTARTS = 10
FULL_ITER = 300

# ── Soglie decisionali PRE-REGISTRATE ───────────────────────────────────────
MATERIALITY_NAT_PER_BAR = 0.05   # 3 stati "vince" se media held-out >= questo
SIGN_CONSISTENCY = 0.75          # ...e segno positivo in >= 75% dei fold

# Palette categorica validata (slot 1-3, all-pairs PASS in light mode)
STATE_COLORS = {"range": "#2a78d6", "trend": "#eb6834", "transition": "#1baf7a"}
INK, MUTED = "#0b0b0b", "#52514e"


# ── Caricamento ─────────────────────────────────────────────────────────────
def load_features(asset: str, feature_names=None):
    names = list(feature_names or rf.FEATURE_NAMES)
    X, closes, times = [], [], []
    with (FEATURES_DIR / f"{asset}_features.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            X.append([float(r[n]) for n in names])
            closes.append(float(r["close"]))
            times.append(int(r["open_time_ms"]))
    return np.asarray(X, float), np.asarray(closes), np.asarray(times)


def fit_hmm(X_std: np.ndarray, n_states: int, restarts: int, n_iter: int):
    """Fit con restart multipli; SELEZIONE SUL SOLO TRAIN (mai sul test)."""
    best, best_ll = None, -math.inf
    for seed in range(restarts):
        m = hmm.GaussianHMM(n_components=n_states, covariance_type="diag",
                            n_iter=n_iter, tol=1e-4, random_state=seed)
        try:
            m.fit(X_std)
            ll = m.score(X_std)
        except Exception:
            continue
        if math.isfinite(ll) and ll > best_ll:
            best, best_ll = m, ll
    if best is None:
        raise RuntimeError(f"nessun fit per {n_states} stati")
    return best, best_ll


def diag_vars(model):
    return np.array([np.diag(c) for c in model.covars_])


def to_model_dict(model, scaler_mean, scaler_std, labels=None):
    """Serializza nel formato letto dallo scorer puro regime_hmm."""
    return {
        "n_states": int(model.n_components),
        "n_features": int(model.means_.shape[1]),
        "startprob": model.startprob_.tolist(),
        "transmat": model.transmat_.tolist(),
        "means": model.means_.tolist(),
        "vars": diag_vars(model).tolist(),
        "scaler_mean": scaler_mean.tolist(),
        "scaler_std": scaler_std.tolist(),
        "labels": labels or {},
    }


def label_by_adx(centroids_orig: np.ndarray, adx_idx: int) -> dict:
    """Etichetta gli stati ordinandoli per ADX: basso=range, alto=trend."""
    order = list(np.argsort(centroids_orig[:, adx_idx]))
    labels = {str(order[0]): "range", str(order[-1]): "trend"}
    for mid in order[1:-1]:
        labels[str(mid)] = "transition"
    return labels


# ── A. Anatomia del terzo stato ─────────────────────────────────────────────
def third_state_anatomy(m2, m3) -> dict:
    """Identifica lo stato 'nuovo' del modello a 3 stati e cosa lo separa.

    Metodo: ogni centroide del modello a 2 stati viene appaiato al centroide piu'
    vicino tra i 3 (in spazio standardizzato). Il centroide a 3 stati rimasto
    spaiato E' lo stato nuovo. Poi si misura, feature per feature, quanto quel
    centroide dista dalla media degli altri due: la feature col contributo
    maggiore alla distanza quadratica e' quella che "crea" il terzo stato.
    """
    c2, c3 = m2.means_, m3.means_          # entrambi in spazio standardizzato
    matched = set()
    for centroid in c2:
        d = np.linalg.norm(c3 - centroid, axis=1)
        for cand in np.argsort(d):
            if cand not in matched:
                matched.add(int(cand))
                break
    new_state = next(s for s in range(3) if s not in matched)

    others = np.delete(c3, new_state, axis=0).mean(axis=0)
    delta = c3[new_state] - others                 # differenza per feature
    contrib = delta ** 2
    share = contrib / contrib.sum()
    return {
        "new_state": new_state,
        "delta_per_feature": delta.tolist(),
        "share_per_feature": share.tolist(),
        "dominant": rf.FEATURE_NAMES[int(np.argmax(share))],
    }


# ── B. Walk-forward expanding (nessun leakage nello scaler) ─────────────────
def walk_forward(X: np.ndarray) -> list[dict]:
    """Delta-LL per barra held-out (3 stati - 2 stati), fold per fold.

    LEAKAGE: media e deviazione standard sono calcolate SOLO sul blocco di
    training del fold e poi applicate al test. Il test non contribuisce mai allo
    scaler ne' alla scelta del restart.
    """
    n = len(X)
    initial_train = n - N_FOLDS * TEST_BLOCK
    if initial_train < 2000:
        raise RuntimeError("storico insufficiente per il protocollo dichiarato")

    results = []
    for k in range(N_FOLDS):
        tr_end = initial_train + k * TEST_BLOCK
        te_end = tr_end + TEST_BLOCK
        X_tr, X_te = X[:tr_end], X[tr_end:te_end]

        # >>> scaler fittato SOLO sul train del fold <<<
        mu, sd = X_tr.mean(axis=0), X_tr.std(axis=0, ddof=0)
        Xtr_s, Xte_s = (X_tr - mu) / sd, (X_te - mu) / sd

        fold = {"fold": k + 1, "train_end": tr_end, "n_test": len(X_te)}
        for n_states in (2, 3):
            model, _ = fit_hmm(Xtr_s, n_states, WF_RESTARTS, WF_ITER)
            fold[f"ll_{n_states}"] = float(model.score(Xte_s)) / len(X_te)
        fold["delta"] = fold["ll_3"] - fold["ll_2"]
        results.append(fold)
    return results


# ── D. Usabilita' live: online-filtrato vs Viterbi retrospettivo ────────────
def episode_lengths(states: np.ndarray, n_states: int) -> dict:
    """Durata media degli 'episodi' (run consecutivi) per stato."""
    runs = {s: [] for s in range(n_states)}
    cur, length = states[0], 1
    for s in states[1:]:
        if s == cur:
            length += 1
        else:
            runs[int(cur)].append(length)
            cur, length = s, 1
    runs[int(cur)].append(length)
    return {s: (float(np.mean(v)) if v else 0.0) for s, v in runs.items()}


def live_usability(model_dict: dict, X: np.ndarray, burn_in: int = 200) -> dict:
    """Confronta lo stato FILTRATO online con quello Viterbi retrospettivo.

    Usa regime_hmm (lo scorer puro del bot) per la parte online, cosi' le metriche
    descrivono esattamente cio' che la produzione calcolera'.
    """
    X_list = X.tolist()
    filt = np.asarray(regime_hmm.filtered_posteriors(model_dict, X_list))
    online = filt.argmax(axis=1)
    viterbi = np.asarray(regime_hmm.predict(model_dict, X_list))

    n_states = model_dict["n_states"]
    o, v = online[burn_in:], viterbi[burn_in:]
    disagree = o != v
    # Gli array completi tornano al chiamante, che li riusa per i grafici invece
    # di rifare due passaggi costosi sull'intera serie.

    per_state = {}
    for s in range(n_states):
        sel = o == s          # quanto disaccordo cade su ciascuno stato ONLINE
        per_state[s] = {
            "share_of_disagreement": float(disagree[sel].sum() / max(disagree.sum(), 1)),
            "error_rate_when_online_says_s": float(disagree[sel].mean()) if sel.any() else 0.0,
        }

    churn = float((o[1:] != o[:-1]).mean())   # frequenza di cambio stato online
    return {
        "disagreement_rate": float(disagree.mean()),
        "per_state": per_state,
        "online_churn_per_bar": churn,
        "episode_len_online": episode_lengths(o, n_states),
        "episode_len_viterbi": episode_lengths(v, n_states),
        "mean_confidence": float(filt[burn_in:].max(axis=1).mean()),
        "_states_online": online,
        "_states_viterbi": viterbi,
    }


def sequence_sensitivity(model_dict: dict, X: np.ndarray,
                         lengths=(50, 100, 250, 500)) -> dict:
    """Quanto la decisione online all'ultima barra dipende dalla lunghezza di
    sequenza fornita. Serve a fissare su evidenza quante barre dovra' passare
    regime_detector.py in live.

    Riferimento: sequenza piena. Per una serie di punti finali, misura quanto
    spesso lo stato filtrato con L osservazioni coincide con quello ottenuto
    usando tutta la storia disponibile fino a quel punto.
    """
    endpoints = list(range(2000, len(X), 1000))
    # Il riferimento a storia piena si calcola UNA volta per endpoint: e' il
    # passaggio costoso, e non dipende da L.
    reference = [regime_hmm.predict_regime(model_dict, X[:e].tolist()) for e in endpoints]

    out = {}
    for L in lengths:
        agree, conf_err = 0, []
        for ref, e in zip(reference, endpoints):
            part = regime_hmm.predict_regime(model_dict, X[max(0, e - L):e].tolist())
            agree += int(ref["state"] == part["state"])
            conf_err.append(abs(ref["confidence"] - part["confidence"]))
        out[L] = {
            "state_agreement": agree / len(endpoints),
            "mean_confidence_abs_error": float(np.mean(conf_err)),
        }
    return out


# ── E. Grafici ──────────────────────────────────────────────────────────────
def plot_asset(asset, times, closes, panels, path: Path):
    """Prezzo con sfondo per regime. Per ogni configurazione due strisce:
    ONLINE (cio' che il bot vedrebbe) e VITERBI (retrospettivo)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime, timezone

    dates = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in times]
    fig, axes = plt.subplots(len(panels), 1, figsize=(14, 4.2 * len(panels)),
                             sharex=True)
    axes = np.atleast_1d(axes)

    for ax, panel in zip(axes, panels):
        states, labels, title = panel["online"], panel["labels"], panel["title"]
        # Sfondo: bande verticali colorate per stato online (cio' che vede il bot)
        start = 0
        for i in range(1, len(states) + 1):
            if i == len(states) or states[i] != states[start]:
                lab = labels.get(str(int(states[start])), "?")
                ax.axvspan(dates[start], dates[i - 1],
                           color=STATE_COLORS.get(lab, "#999"), alpha=0.18, lw=0)
                start = i
        ax.plot(dates, closes, color=INK, lw=1.0)          # marca sottile
        ax.set_title(title, fontsize=11, color=INK, loc="left")
        ax.set_ylabel("prezzo USD", fontsize=9, color=MUTED)
        ax.grid(True, alpha=0.15, lw=0.6)                   # griglia recessiva
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        # Striscia Viterbi sotto, per rendere visibile il disaccordo
        vit = panel["viterbi"]
        y0, y1 = ax.get_ylim()
        h = (y1 - y0) * 0.045
        start = 0
        for i in range(1, len(vit) + 1):
            if i == len(vit) or vit[i] != vit[start]:
                lab = labels.get(str(int(vit[start])), "?")
                ax.fill_between([dates[start], dates[i - 1]], y0 - h * 1.6, y0 - h * 0.6,
                                color=STATE_COLORS.get(lab, "#999"), lw=0)
                start = i
        ax.set_ylim(y0 - h * 2.0, y1)
        ax.text(dates[0], y0 - h * 1.1, " Viterbi (retrospettivo) ",
                fontsize=7, color=MUTED, va="center")

    # Legenda: identita' mai affidata al solo colore
    handles = [plt.Line2D([0], [0], marker="s", ls="", markersize=9,
                          color=STATE_COLORS[k], label=k)
               for k in ("range", "transition", "trend") if k in STATE_COLORS]
    axes[0].legend(handles=handles, loc="upper left", frameon=False, fontsize=9,
                   ncol=3)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.suptitle(f"{asset} · regime sullo sfondo, prezzo 1h · "
                 f"bande = stato ONLINE (forward filtering)",
                 fontsize=12, color=INK, x=0.5, y=0.995)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ── Orchestrazione ──────────────────────────────────────────────────────────
def validate_asset(asset: str, make_plots: bool) -> dict:
    X, closes, times = load_features(asset)
    n, d = X.shape
    adx_idx = list(rf.FEATURE_NAMES).index("adx")
    mu, sd = X.mean(axis=0), X.std(axis=0, ddof=0)
    X_std = (X - mu) / sd

    print(f"\n{'='*74}\n{asset} · {n} osservazioni\n{'='*74}")

    # Modelli full-history (stesso protocollo di train_model.py)
    m2, ll2 = fit_hmm(X_std, 2, FULL_RESTARTS, FULL_ITER)
    m3, ll3 = fit_hmm(X_std, 3, FULL_RESTARTS, FULL_ITER)

    # ── A. anatomia del terzo stato ──
    anat = third_state_anatomy(m2, m3)
    print(f"\n[A] Anatomia del terzo stato  (stato nuovo = {anat['new_state']})")
    print(f"    {'feature':14s} {'Δ vs altri 2':>13s} {'quota separaz.':>15s}")
    for i, name in enumerate(rf.FEATURE_NAMES):
        print(f"    {name:14s} {anat['delta_per_feature'][i]:>+13.3f} "
              f"{anat['share_per_feature'][i]*100:>14.1f}%")
    print(f"    -> feature dominante: {anat['dominant']}")

    print("\n    Centroidi standardizzati (3 stati):")
    for s in range(3):
        vals = "  ".join(f"{nm}={m3.means_[s][i]:+.2f}"
                         for i, nm in enumerate(rf.FEATURE_NAMES))
        print(f"      stato {s}: {vals}")

    # ── A2. controprova: solo ADX + KER ──
    X2, _, _ = load_features(asset, ["adx", "kaufman_er"])
    mu2, sd2 = X2.mean(axis=0), X2.std(axis=0, ddof=0)
    X2_std = (X2 - mu2) / sd2
    a2, lla2 = fit_hmm(X2_std, 2, FULL_RESTARTS, FULL_ITER)
    a3, lla3 = fit_hmm(X2_std, 3, FULL_RESTARTS, FULL_ITER)
    gain_4f = (ll3 - ll2) / n
    gain_2f = (lla3 - lla2) / n
    print(f"\n[A2] Controprova — guadagno in-sample del 3° stato (nat/barra):")
    print(f"     4 feature (ADX,ATR%,KER,vol): {gain_4f:.4f}")
    print(f"     solo direzionalita' (ADX,KER): {gain_2f:.4f}"
          f"   -> residuo {gain_2f/gain_4f*100:.1f}% del guadagno")

    # ── B. walk-forward ──
    folds = walk_forward(X)
    deltas = np.array([f["delta"] for f in folds])
    pos = float((deltas > 0).mean())
    print(f"\n[B] Walk-forward expanding · {N_FOLDS} fold × {TEST_BLOCK} barre "
          f"held-out (scaler per-fold)")
    print(f"    {'fold':>4s} {'LL/barra 2st':>13s} {'LL/barra 3st':>13s} {'Δ (3-2)':>10s}")
    for f in folds:
        print(f"    {f['fold']:>4d} {f['ll_2']:>13.4f} {f['ll_3']:>13.4f} "
              f"{f['delta']:>+10.4f}")
    print(f"    media Δ = {deltas.mean():+.4f} nat/barra · dev.std = {deltas.std():.4f} "
          f"· min {deltas.min():+.4f} · max {deltas.max():+.4f}")
    print(f"    segno positivo in {pos*100:.0f}% dei fold "
          f"(soglia {SIGN_CONSISTENCY*100:.0f}%) · materialita' {MATERIALITY_NAT_PER_BAR}")
    wins_wf = bool(deltas.mean() >= MATERIALITY_NAT_PER_BAR and pos >= SIGN_CONSISTENCY)
    print(f"    -> 3 stati {'SUPERA' if wins_wf else 'NON supera'} la soglia held-out")

    # ── C+D. struttura e usabilita' live ──
    results = {"asset": asset, "anatomy": anat, "folds": folds,
               "wf_mean_delta": float(deltas.mean()), "wf_sign_pos": pos,
               "wf_wins": wins_wf, "gain_4f": gain_4f, "gain_2f": gain_2f}
    panels = []
    for model, n_states in ((m2, 2), (m3, 3)):
        cent = model.means_ * sd + mu
        labels = label_by_adx(cent, adx_idx)
        md = to_model_dict(model, mu, sd, labels)
        live = live_usability(md, X)
        states_online = live.pop("_states_online")
        states_vit = live.pop("_states_viterbi")

        print(f"\n[D] {n_states} stati · usabilita' live")
        print(f"    disaccordo online-vs-Viterbi: {live['disagreement_rate']*100:.1f}% "
              f"delle barre · confidence media {live['mean_confidence']:.3f}")
        print(f"    churn online (cambi di stato per barra): "
              f"{live['online_churn_per_bar']*100:.2f}%")
        for s in range(n_states):
            lab = labels[str(s)]
            print(f"      stato {s} '{lab:10s}' episodio medio ONLINE "
                  f"{live['episode_len_online'][s]:5.1f}h vs Viterbi "
                  f"{live['episode_len_viterbi'][s]:5.1f}h · "
                  f"quota disaccordo {live['per_state'][s]['share_of_disagreement']*100:4.1f}% "
                  f"· errore quando dice '{lab}' "
                  f"{live['per_state'][s]['error_rate_when_online_says_s']*100:.1f}%")
        results[f"live_{n_states}"] = live
        panels.append({"online": states_online, "viterbi": states_vit,
                       "labels": labels, "title": f"{n_states} stati"})

        if n_states == 3:
            sens = sequence_sensitivity(md, X)
            print(f"\n[D2] {n_states} stati · sensibilita' alla lunghezza di sequenza "
                  f"(decisione all'ultima barra vs storia piena)")
            for L, v in sens.items():
                print(f"      {L:>4d} osservazioni: accordo stato "
                      f"{v['state_agreement']*100:5.1f}% · errore medio confidence "
                      f"{v['mean_confidence_abs_error']:.4f}")
            results["seq_sensitivity"] = sens

    if make_plots:
        out = REPORTS_DIR / f"{asset}_regimes.png"
        plot_asset(asset, times, closes, panels, out)
        print(f"\n[E] grafico -> {out}")
        results["plot"] = str(out)
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Validazione HMM di regime (step 4)")
    p.add_argument("--assets", nargs="+", default=list(ASSETS), choices=list(ASSETS))
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args(argv)

    all_results = [validate_asset(a, not args.no_plots) for a in args.assets]
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "validation.json").write_text(
        json.dumps(all_results, indent=2, default=float), encoding="utf-8")

    print(f"\n{'='*74}\nRIEPILOGO held-out (criterio 3)\n{'='*74}")
    for r in all_results:
        print(f"  {r['asset']}: Δ medio {r['wf_mean_delta']:+.4f} nat/barra · "
              f"segno+ {r['wf_sign_pos']*100:.0f}% · "
              f"{'supera' if r['wf_wins'] else 'NON supera'} la soglia")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
