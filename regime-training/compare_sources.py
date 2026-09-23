"""compare_sources.py — discrepanza di fonte fra training (Coinbase) e live.

Il modello e' addestrato su candele Coinbase. Se in produzione il detector
leggesse un'altra fonte, si reintrodurrebbe train/serve skew dalla porta dei
dati — la stessa classe di problema che hash sulle feature e parita' dello
scorer hanno eliminato altrove. Questo script la misura invece di assumerla.

Su una finestra recente sovrapposta, per ogni asset:
  1. scarica le stesse barre 1h da Coinbase (paginato) e da Kraken;
  2. allinea per timestamp e confronta OHLCV;
  3. calcola le 4 feature su CIASCUNA fonte con la finestra canonica;
  4. confronta le feature (mediana, p95, max dello scarto assoluto);
  5. esegue il modello 2 stati su entrambe e misura il disaccordo di regime
     filtrato (la metrica che conta davvero: quante ore il bot chiamerebbe un
     regime diverso solo per aver cambiato fonte).

Va rilanciato a ogni retraining, o prima di cambiare fonte dati in produzione.
"""

from __future__ import annotations

import argparse, json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import regime_features as rf   # noqa: E402
import regime_hmm              # noqa: E402

COINBASE_BASE = "https://api.exchange.coinbase.com"
KRAKEN_BASE = "https://api.kraken.com/0/public"
COINBASE_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
GRAN = 3600
ARTIFACTS = Path(__file__).parent / "artifacts"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "liquid-bot/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def coinbase_bars(asset: str, n: int) -> dict[int, dict]:
    """n barre 1h recenti da Coinbase, paginando all'indietro (300/richiesta)."""
    out, end = {}, datetime.now(timezone.utc)
    while len(out) < n:
        start = end - timedelta(seconds=GRAN * 300)
        q = urllib.parse.urlencode({"granularity": GRAN,
                                    "start": start.isoformat(), "end": end.isoformat()})
        rows = _get(f"{COINBASE_BASE}/products/{COINBASE_PRODUCTS[asset]}/candles?{q}")
        if not rows:
            break
        for r in rows:   # [time, low, high, open, close, volume]
            out[int(r[0])] = {"open": float(r[3]), "high": float(r[2]),
                              "low": float(r[1]), "close": float(r[4]),
                              "volume": float(r[5])}
        end = datetime.fromtimestamp(min(int(r[0]) for r in rows), tz=timezone.utc)
        time.sleep(0.25)
    return out


def kraken_bars(asset: str) -> dict[int, dict]:
    """Kraken restituisce in una sola chiamata le ~720 barre 1h piu' recenti."""
    d = _get(f"{KRAKEN_BASE}/OHLC?pair={KRAKEN_PAIRS[asset]}&interval=60")
    if d.get("error"):
        raise RuntimeError(f"Kraken: {d['error']}")
    res = d["result"]
    key = next(k for k in res if k != "last")
    return {int(r[0]): {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                        "close": float(r[4]), "volume": float(r[6])} for r in res[key]}


def feature_series(bars: dict[int, dict]) -> dict[int, list]:
    """Feature per ogni barra la cui finestra canonica precedente e' contigua."""
    ts = sorted(bars)
    W = rf.FEATURE_WINDOW_BARS
    out = {}
    for i in range(W - 1, len(ts)):
        window_ts = ts[i - W + 1: i + 1]
        if any(b - a != GRAN for a, b in zip(window_ts, window_ts[1:])):
            continue
        f = rf.compute_features([bars[t] for t in window_ts])
        if f:
            out[ts[i]] = [f[n] for n in rf.FEATURE_NAMES]
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Confronto fonte Coinbase vs Kraken")
    p.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL"])
    p.add_argument("--bars", type=int, default=720)
    args = p.parse_args(argv)

    for asset in args.assets:
        print(f"\n{'='*72}\n{asset}\n{'='*72}")
        try:
            cb, kr = coinbase_bars(asset, args.bars), kraken_bars(asset)
        except Exception as exc:
            print(f"  fetch fallito: {exc}")
            continue

        common = sorted(set(cb) & set(kr))
        print(f"  barre: Coinbase {len(cb)} · Kraken {len(kr)} · sovrapposte {len(common)}")
        if len(common) < rf.FEATURE_WINDOW_BARS + 30:
            print("  sovrapposizione insufficiente per il confronto feature")
            continue

        # 1) scarto sui prezzi grezzi (close) e sul volume
        c_close = np.array([cb[t]["close"] for t in common])
        k_close = np.array([kr[t]["close"] for t in common])
        rel = np.abs(c_close - k_close) / c_close * 1e4          # in basis point
        c_vol = np.array([cb[t]["volume"] for t in common])
        k_vol = np.array([kr[t]["volume"] for t in common])
        print(f"  close  scarto bps: mediana {np.median(rel):.2f} · "
              f"p95 {np.percentile(rel,95):.2f} · max {rel.max():.2f}")
        print(f"  volume rapporto Kraken/Coinbase: mediana {np.median(k_vol/c_vol):.3f}")

        # 2) feature calcolate su ciascuna fonte, allineate per timestamp
        fc, fk = feature_series(cb), feature_series(kr)
        shared = sorted(set(fc) & set(fk))
        print(f"  righe feature confrontabili: {len(shared)}")
        if not shared:
            continue
        A = np.array([fc[t] for t in shared])
        B = np.array([fk[t] for t in shared])
        print(f"\n  {'feature':14s} {'mediana |Δ|':>12s} {'p95 |Δ|':>10s} {'max |Δ|':>10s} "
              f"{'mediana |Δ| in σ':>17s}")
        sd = A.std(axis=0)
        for i, n in enumerate(rf.FEATURE_NAMES):
            d = np.abs(A[:, i] - B[:, i])
            print(f"  {n:14s} {np.median(d):>12.4f} {np.percentile(d,95):>10.4f} "
                  f"{d.max():>10.4f} {np.median(d)/sd[i]:>17.3f}")

        # 3) la metrica che conta: disaccordo di REGIME fra le due fonti
        model = json.loads((ARTIFACTS / f"{asset}_hmm_2.json").read_text())
        sa = np.array(regime_hmm.filtered_posteriors(model, A.tolist())).argmax(axis=1)
        sb = np.array(regime_hmm.filtered_posteriors(model, B.tolist())).argmax(axis=1)
        dis = (sa != sb).mean()
        labels = model.get("labels", {})
        print(f"\n  DISACCORDO DI REGIME fra fonti: {dis*100:.2f}% delle ore "
              f"({int((sa!=sb).sum())}/{len(sa)})")
        for s in range(model["n_states"]):
            share_a = (sa == s).mean(); share_b = (sb == s).mean()
            print(f"    '{labels.get(str(s),s):10s}' quota Coinbase {share_a*100:5.1f}% "
                  f"· Kraken {share_b*100:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
