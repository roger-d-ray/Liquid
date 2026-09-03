"""build_dataset.py — Passo 2b: calcola le feature di regime su tutto lo storico.

Per ogni asset legge ``data/history/{ASSET}_1h.csv``, fa scorrere la finestra
canonica (``regime_features.FEATURE_WINDOW_BARS``) barra per barra e, per ogni
barra il cui intero window e' contiguo nel tempo (nessun buco orario), calcola le
4 feature con la SORGENTE UNICA ``regime_features.compute_features``. Il risultato
va in ``data/features/{ASSET}_features.csv``: e' la tabella su cui, nello step 3,
addestreremo un HMM per asset.

Scelte di robustezza (parita' train/serve e onesta' dei dati):
- Finestra di FEATURE_WINDOW_BARS barre finali: identica a quella che il detector
  live passera' a compute_features, cosi' i valori coincidono.
- Finestra a cavallo di un buco -> riga scartata: meglio perdere qualche riga che
  passare al modello un ATR/ADX distorto dal salto di prezzo sul buco.
- Nessuna feature inventata: se compute_features ritorna None, la riga si salta.

Solo lettura di CSV locali + scrittura di CSV locali. Nessuna dipendenza esterna.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

# Il file gira dentro regime-training/, quindi la sua cartella e' gia' in
# sys.path e ``import regime_features`` risolve la sorgente unica accanto.
sys.path.insert(0, str(Path(__file__).parent))
import regime_features as rf  # noqa: E402

HISTORY_DIR = Path(__file__).parent / "data" / "history"
FEATURES_DIR = Path(__file__).parent / "data" / "features"
STEP_MS = 3600 * 1000  # 1 ora in millisecondi: il passo atteso tra barre 1h
ASSETS = ("BTC", "ETH", "SOL")


def _load_history(asset: str) -> list[dict]:
    """Legge il CSV storico in una lista cronologica di candele (float)."""
    path = HISTORY_DIR / f"{asset}_1h.csv"
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [
            {
                "open_time_ms": int(r["open_time_ms"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
            for r in reader
        ]


def _is_contiguous(window: list[dict]) -> bool:
    """True se ogni coppia di barre consecutive dista esattamente 1 ora.

    Serve a escludere le finestre che scavalcano un buco: dentro c'e' un salto di
    prezzo tra due barre non adiacenti nel tempo, che falserebbe ATR/ADX.
    """
    return all(
        window[j + 1]["open_time_ms"] - window[j]["open_time_ms"] == STEP_MS
        for j in range(len(window) - 1)
    )


def build_asset(asset: str) -> tuple[list[dict], dict]:
    """Calcola le feature per un asset. Ritorna (righe, statistiche)."""
    candles = _load_history(asset)
    window_bars = rf.FEATURE_WINDOW_BARS

    rows: list[dict] = []
    skipped_gap = 0
    skipped_none = 0

    # La prima finestra completa termina all'indice window_bars-1 (warm-up).
    for end in range(window_bars - 1, len(candles)):
        window = candles[end - window_bars + 1 : end + 1]
        if not _is_contiguous(window):
            skipped_gap += 1
            continue
        feat = rf.compute_features(window)
        if feat is None:
            skipped_none += 1
            continue
        bar = candles[end]
        rows.append(
            {
                "open_time_ms": bar["open_time_ms"],
                "close": bar["close"],
                **{name: feat[name] for name in rf.FEATURE_NAMES},
            }
        )

    stats = {
        "total_bars": len(candles),
        "warmup_skipped": window_bars - 1,
        "gap_skipped": skipped_gap,
        "none_skipped": skipped_none,
        "rows": len(rows),
    }
    return rows, stats


def write_features(asset: str, rows: list[dict]) -> Path:
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FEATURES_DIR / f"{asset}_features.csv"
    fields = ["open_time_ms", "close", *rf.FEATURE_NAMES]
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return out


def _describe(rows: list[dict]) -> str:
    """Riga di min/mediana/max per ogni feature: controllo a occhio della sanita'."""
    parts = []
    for name in rf.FEATURE_NAMES:
        vals = [r[name] for r in rows]
        parts.append(
            f"{name}[{min(vals):.3f} / {statistics.median(vals):.3f} / {max(vals):.3f}]"
        )
    return "  ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calcola le feature di regime su tutto lo storico (per asset)"
    )
    parser.add_argument("--assets", nargs="+", default=list(ASSETS), choices=list(ASSETS))
    args = parser.parse_args(argv)

    print(f"Finestra canonica: {rf.FEATURE_WINDOW_BARS} barre · "
          f"feature: {', '.join(rf.FEATURE_NAMES)}\n")
    exit_code = 0
    for asset in args.assets:
        try:
            rows, st = build_asset(asset)
        except FileNotFoundError:
            print(f"[{asset}] storico mancante: lancia prima fetch_history.py\n")
            exit_code = 1
            continue
        if not rows:
            print(f"[{asset}] nessuna riga di feature prodotta\n")
            exit_code = 1
            continue
        out = write_features(asset, rows)
        print(
            f"[{asset}] {st['rows']} righe feature "
            f"(su {st['total_bars']} barre: -{st['warmup_skipped']} warm-up, "
            f"-{st['gap_skipped']} a cavallo buchi, -{st['none_skipped']} incalcolabili)"
        )
        print(f"        min/mediana/max: {_describe(rows)}")
        print(f"        -> {out}\n")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
