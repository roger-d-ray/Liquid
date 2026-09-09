"""plot_regimes.py — conferma visiva leggibile del regime (zoom su finestra).

Il grafico su 2 anni interi non e' utilizzabile come conferma visiva: con regimi
orari le bande sono larghe un pixel e il risultato e' un codice a barre. Questo
script rende una finestra ristretta (default 90 giorni), dove i singoli episodi
di regime sono distinguibili e si puo' davvero giudicare se gli stati "trend"
cadono sui tratti direzionali.

Usa i modelli gia' addestrati in artifacts/ (nessun re-fit) e lo scorer puro
regime_hmm: le bande mostrano lo stato ONLINE (forward filtering), cioe' cio' che
il bot vedrebbe in produzione, non la ricostruzione retrospettiva di Viterbi.
Le posteriori sono calcolate sull'INTERA storia precedente e poi ritagliate, come
in live.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates      # noqa: E402
import matplotlib.pyplot as plt        # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import regime_features as rf           # noqa: E402
import regime_hmm                      # noqa: E402

FEATURES_DIR = Path(__file__).parent / "data" / "features"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
REPORTS_DIR = ARTIFACTS_DIR / "reports"

# Palette categorica validata (slot 1-3: CVD e normal-vision PASS all-pairs)
STATE_COLORS = {"range": "#2a78d6", "trend": "#eb6834", "transition": "#1baf7a"}
INK, MUTED = "#0b0b0b", "#52514e"


def load_series(asset: str):
    X, closes, times = [], [], []
    with (FEATURES_DIR / f"{asset}_features.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            X.append([float(r[n]) for n in rf.FEATURE_NAMES])
            closes.append(float(r["close"]))
            times.append(int(r["open_time_ms"]))
    return X, closes, times


def shade(ax, dates, states, labels, y0, y1):
    """Bande verticali per episodio di regime, con 2px di respiro fra i blocchi."""
    start = 0
    for i in range(1, len(states) + 1):
        if i == len(states) or states[i] != states[start]:
            lab = labels.get(str(int(states[start])), "?")
            ax.axvspan(dates[start], dates[i - 1],
                       color=STATE_COLORS.get(lab, "#999"), alpha=0.22, lw=0)
            start = i


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Zoom leggibile del regime online")
    p.add_argument("--asset", default="BTC")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--end", type=str, default=None,
                   help="fine finestra ISO (default: ultima barra disponibile)")
    args = p.parse_args(argv)

    X, closes, times = load_series(args.asset)

    # Finestra da mostrare: le posteriori restano calcolate su tutta la storia
    # precedente (come in live), poi si ritaglia solo la parte da disegnare.
    bars = args.days * 24
    end_idx = len(X)
    if args.end:
        target = int(datetime.fromisoformat(args.end).replace(
            tzinfo=timezone.utc).timestamp() * 1000)
        end_idx = max((i for i, t in enumerate(times) if t <= target), default=len(X) - 1) + 1
    start_idx = max(0, end_idx - bars)

    dates = [datetime.fromtimestamp(t / 1000, tz=timezone.utc)
             for t in times[start_idx:end_idx]]
    prices = closes[start_idx:end_idx]

    fig, axes = plt.subplots(2, 1, figsize=(14, 7.5), sharex=True)
    for ax, n_states in zip(axes, (2, 3)):
        model = json.loads(
            (ARTIFACTS_DIR / f"{args.asset}_hmm_{n_states}.json").read_text())
        labels = model.get("labels", {})
        post = regime_hmm.filtered_posteriors(model, X[:end_idx])
        states = [max(range(len(r)), key=lambda s: r[s]) for r in post][start_idx:end_idx]

        ax.plot(dates, prices, color=INK, lw=1.4)          # marca sottile
        y0, y1 = ax.get_ylim()
        shade(ax, dates, states, labels, y0, y1)
        ax.set_ylim(y0, y1)
        ax.set_title(f"{n_states} stati", fontsize=11, color=INK, loc="left")
        ax.set_ylabel("prezzo USD", fontsize=9, color=MUTED)
        ax.grid(True, alpha=0.15, lw=0.6)                  # griglia recessiva
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=9)

        present = []
        for lab in ("range", "transition", "trend"):
            if any(labels.get(str(s)) == lab for s in range(n_states)):
                present.append(lab)
        # Legenda sempre presente: l'identita' non e' mai affidata al solo colore
        ax.legend(handles=[plt.Line2D([0], [0], marker="s", ls="", markersize=10,
                                      color=STATE_COLORS[l], label=l)
                           for l in present],
                  loc="upper left", frameon=False, fontsize=9, ncol=3)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.suptitle(
        f"{args.asset} · ultimi {args.days} giorni · bande = regime ONLINE "
        f"(forward filtering, cio' che il bot vedrebbe)",
        fontsize=12, color=INK)
    fig.tight_layout()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{args.asset}_zoom_{args.days}d.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
