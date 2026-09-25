"""fetch_history.py — Passo 1: storico OHLCV 1h per il training.

Scarica ~2 anni di barre 1h di BTC/ETH/SOL e le salva in data/history/.

Usa regime_source.py — la stessa sorgente unica del detector live — per fonte,
parser e paginazione: la stessa riga Coinbase viene interpretata dallo stesso
codice in training e in produzione. (Kraken non puo' servire qui: il suo endpoint
OHLC restituisce solo ~720 barre e non pagina all'indietro. DECISIONS.md §3.)

PROVENIENZA: per ogni asset scrive in data/history/provenance.json la fonte, la
granularita' e l'SHA-256 di regime_source.py IN USO AL MOMENTO DEL FETCH.
train_model.py la porta dentro il modello; export_model.py e il detector la
verificano. Se qualcuno cambia fonte senza riaddestrare, il sistema tace.

I buchi della fonte (manutenzioni Coinbase) restano buchi: vengono registrati,
mai riempiti. build_dataset.py scarta le finestre che li attraversano.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import regime_source as rs  # noqa: E402

HERE = Path(__file__).parent
DEFAULT_OUTDIR = HERE / "data" / "history"
G = rs.GRANULARITY_SECONDS


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_history(asset: str, *, years: float, now: float | None = None) -> list[dict]:
    """Barre chiuse degli ultimi `years` anni, cronologiche. Buchi ammessi."""
    end_open = rs.last_closed_open_time(time.time() if now is None else now)
    n = int(years * 365 * 24)
    first_open = end_open - (n - 1) * G
    collected, _ = rs.fetch_range(asset, first_open, end_open)
    return [collected[t] for t in sorted(collected)]


def detect_gaps(candles: list[dict]) -> list[tuple[int, int]]:
    step = G * 1000
    return [(p["open_time_ms"], n["open_time_ms"]) for p, n in zip(candles, candles[1:])
            if n["open_time_ms"] - p["open_time_ms"] != step]


def write_csv(path: Path, candles: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["open_time_ms", "open_time_iso", "open", "high", "low", "close", "volume"])
        for c in candles:
            iso = datetime.fromtimestamp(c["open_time_ms"] / 1000, tz=timezone.utc).isoformat()
            w.writerow([c["open_time_ms"], iso, c["open"], c["high"], c["low"],
                        c["close"], c["volume"]])


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Storico 1h per il training (Coinbase)")
    p.add_argument("--years", type=float, default=2.0)
    p.add_argument("--assets", nargs="+", default=list(rs.PRODUCTS), choices=list(rs.PRODUCTS))
    p.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    args = p.parse_args(argv)

    source_sha = sha256_file(HERE / "regime_source.py")
    prov_path = args.outdir / "provenance.json"
    provenance = json.loads(prov_path.read_text()) if prov_path.exists() else {}

    print(f"Storico 1h · {args.years} anni · fonte {rs.SOURCE_ID} · "
          f"regime_source.py sha256 {source_sha[:12]}…\n")
    code = 0
    for asset in args.assets:
        print(f"[{asset}] scarico {rs.PRODUCTS[asset]} ...", flush=True)
        try:
            candles = fetch_history(asset, years=args.years)
        except rs.SourceError as exc:
            # Nessun CSV parziale e nessuna provenienza per un asset fallito.
            print(f"[{asset}] ERRORE: {exc}\n", flush=True)
            code = 1
            continue
        gaps = detect_gaps(candles)
        out = args.outdir / f"{asset}_1h.csv"
        write_csv(out, candles)
        provenance[asset] = {
            "data_source": rs.SOURCE_ID,
            "granularity_seconds": G,
            "source_module_sha256": source_sha,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "first_open_time_ms": candles[0]["open_time_ms"],
            "last_open_time_ms": candles[-1]["open_time_ms"],
            "bars": len(candles),
            "gaps": [[a, b] for a, b in gaps],
        }
        span = (candles[-1]["open_time_ms"] - candles[0]["open_time_ms"]) // (G * 1000) + 1
        print(f"[{asset}] {len(candles)} barre · {_fmt(candles[0]['open_time_ms'])} → "
              f"{_fmt(candles[-1]['open_time_ms'])} UTC · buchi {len(gaps)} · "
              f"copertura {len(candles)/span*100:.2f}%\n", flush=True)

    args.outdir.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
