"""analyze_confidence.py — serve a decidere SE mettere una soglia di confidence.

Domanda: sotto una certa confidence conviene non aprire nulla? Si risponde con
tre misure, per asset, sul modello a 2 stati esportato:

  1. distribuzione della confidence (mediana e decili);
  2. tasso di errore per fascia di confidence, dove "errore" = lo stato FILTRATO
     online (cio' che il bot vede) differisce da quello Viterbi retrospettivo
     (cio' che col senno di poi era lo stato piu' probabile);
  3. per ogni soglia candidata: quante ore verrebbero escluse, e quanto migliora
     l'errore sulle ore che restano.

Criterio: se l'errore CROLLA sopra una soglia, quella e' la soglia. Se resta
piatto, la confidence non discrimina e la soglia non si mette — costerebbe
occasioni senza comprare accuratezza.

Il prezzo va letto in occasioni perse, non solo in accuratezza guadagnata.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402

FEATURES_DIR = HERE / "data" / "features"
MODELS_DIR = HERE.parent / "models"
ASSETS = ("BTC", "ETH", "SOL")
BURN_IN = 200
BANDS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90),
         (0.90, 0.95), (0.95, 0.99), (0.99, 1.01)]
THRESHOLDS = (0.60, 0.70, 0.80, 0.90, 0.95, 0.99)


def load_rows(asset: str) -> list[list[float]]:
    with (FEATURES_DIR / f"{asset}_features.csv").open(encoding="utf-8") as fh:
        return [[float(r[n]) for n in rf.FEATURE_NAMES] for r in csv.DictReader(fh)]


def analyse(asset: str) -> dict:
    rows = load_rows(asset)
    model = json.loads((MODELS_DIR / f"regime_{asset}.json").read_text())
    online = np.asarray(regime_hmm.filtered_posteriors(model, rows))
    conf = online.max(axis=1)[BURN_IN:]
    state_online = online.argmax(axis=1)[BURN_IN:]
    state_retro = np.asarray(regime_hmm.predict(model, rows))[BURN_IN:]
    wrong = state_online != state_retro

    print(f"\n{'='*76}\n{asset} · {len(conf)} ore · modello a 2 stati\n{'='*76}")

    q = np.percentile(conf, [10, 20, 30, 40, 50, 60, 70, 80, 90])
    print("  Distribuzione della confidence (decili)")
    print("   " + "  ".join(f"p{d*10}" for d in range(1, 10)))
    print("   " + "  ".join(f"{v:.3f}" for v in q))
    print(f"   mediana {np.median(conf):.4f} · minimo {conf.min():.4f} · "
          f"errore complessivo {wrong.mean()*100:.2f}%")

    print("\n  Errore per fascia di confidence")
    print(f"  {'fascia':>14s} {'ore':>7s} {'% del totale':>13s} {'errore':>9s}")
    for lo, hi in BANDS:
        sel = (conf >= lo) & (conf < hi)
        if not sel.any():
            continue
        print(f"  {f'[{lo:.2f}, {min(hi,1.0):.2f})':>14s} {sel.sum():>7d} "
              f"{sel.mean()*100:>12.1f}% {wrong[sel].mean()*100:>8.2f}%")

    print("\n  Prezzo di ogni soglia candidata")
    print(f"  {'soglia':>7s} {'ore escluse':>12s} {'errore se si applica':>21s} "
          f"{'errore sulle escluse':>21s}")
    base = wrong.mean() * 100
    out_rows = []
    for thr in THRESHOLDS:
        keep, drop = conf >= thr, conf < thr
        kept_err = wrong[keep].mean() * 100 if keep.any() else float("nan")
        drop_err = wrong[drop].mean() * 100 if drop.any() else float("nan")
        print(f"  {thr:>7.2f} {drop.mean()*100:>11.1f}% {kept_err:>20.2f}% "
              f"{drop_err:>20.2f}%")
        out_rows.append({"threshold": thr, "excluded_pct": float(drop.mean() * 100),
                         "error_kept_pct": float(kept_err), "error_dropped_pct": float(drop_err)})
    print(f"  (senza soglia l'errore e' {base:.2f}%)")
    return {"asset": asset, "hours": int(len(conf)), "base_error_pct": float(base),
            "median_confidence": float(np.median(conf)), "thresholds": out_rows}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Analisi della soglia di confidence")
    p.add_argument("--assets", nargs="+", default=list(ASSETS), choices=list(ASSETS))
    args = p.parse_args(argv)
    results = [analyse(a) for a in args.assets]
    out = HERE / "artifacts" / "reports" / "confidence_analysis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
