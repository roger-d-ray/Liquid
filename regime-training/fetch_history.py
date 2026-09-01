"""fetch_history.py — Passo 1 della pipeline di regime detection (SOLO training).

Scarica lo storico OHLCV a 1 ora di BTC/ETH/SOL da Coinbase Exchange e lo salva
in CSV sotto ``regime-training/data/history/``. Da qui i passi successivi
calcoleranno le feature di regime e addestreranno il modello.

Perche' Coinbase e non Kraken
------------------------------
Il bot live usa Kraken come fonte primaria perche' non geo-blocca gli IP cloud,
ma l'endpoint OHLC di Kraken restituisce solo le ~720 barre piu' recenti (a 1h =
~30 giorni) e non permette di risalire indietro nel tempo. Per lo storico
profondo che serve al training l'unica delle fonti pubbliche gratuite che pagina
all'indietro e' Coinbase: accetta una finestra ``start``/``end`` e ne restituisce
fino a 300 candele per richiesta, quindi risaliamo anni indietro una finestra
alla volta. (Binance klines paginerebbe anche meglio, ma da IP cloud risponde
HTTP 451.)

Sicurezza
---------
Solo lettura da API pubblica gratuita: non tocca il conto Co-Invest ne' la
routine live, non ha bisogno di credenziali. Nessuna dipendenza esterna: usa solo
la standard library (urllib), come gia' fa il client Coinbase del bot.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Costanti Coinbase (allineate a data_fetcher.py del bot) ───────────────────
COINBASE_BASE = "https://api.exchange.coinbase.com"
# Coinbase accetta al massimo 300 candele per richiesta: e' il passo con cui
# paginiamo all'indietro.
MAX_CANDLES_PER_REQUEST = 300
# I "prodotti" Coinbase sono coppie contro USD. Manteniamo la mappa esplicita
# invece di sintetizzare il simbolo, cosi' se un asset cambia ticker si vede qui.
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
GRANULARITY_SECONDS = 3600  # 1 ora: il timeframe su cui giudicheremo il regime.

# Pausa tra richieste: il rate limit pubblico di Coinbase e' ~10 req/s; stiamo
# molto sotto per non farci limitare durante una paginazione lunga.
REQUEST_PAUSE_SECONDS = 0.25

DEFAULT_OUTDIR = Path(__file__).parent / "data" / "history"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _request_json(url: str, retries: int = 4):
    """GET con retry+backoff esponenziale su 429/5xx e errori di rete transitori.

    Rispecchia la logica di ``_coinbase_candles`` del bot: gli errori temporanei
    (troppe richieste, 5xx, timeout) si riprovano; un 4xx diverso da 429 (es. 403
    di IP bloccato) e' definitivo e va propagato subito, cosi' lo vediamo.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "liquid-bot/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            transient = exc.code == 429 or 500 <= exc.code < 600
            if transient and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Coinbase HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Coinbase errore di rete: {exc}") from exc
    raise RuntimeError("Coinbase: richiesta fallita dopo i retry")


def _fetch_window(product: str, start: datetime, end: datetime) -> list[list]:
    """Una singola richiesta /candles per la finestra [start, end].

    Coinbase risponde con righe ``[time, low, high, open, close, volume]`` in
    ordine dal piu' recente al piu' vecchio, con ``time`` in secondi epoch.
    """
    params = urllib.parse.urlencode(
        {
            "granularity": GRANULARITY_SECONDS,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    )
    url = f"{COINBASE_BASE}/products/{product}/candles?{params}"
    rows = _request_json(url)
    if not isinstance(rows, list):
        raise RuntimeError(f"Coinbase: risposta inattesa per {product}: {rows!r}")
    return rows


def fetch_history(product: str, *, years: float) -> list[dict]:
    """Scarica ``years`` anni di candele 1h per ``product``, dal piu' vecchio al
    piu' recente, deduplicate.

    Strategia: si parte da adesso e si risale indietro una finestra da 300 barre
    alla volta. Ogni pagina ci dice la barra piu' vecchia che ha restituito: la
    riusiamo come nuovo ``end`` della pagina successiva (il dedup per timestamp
    gestisce la barra di confine ripetuta). Ci si ferma quando abbiamo superato la
    data obiettivo, quando una pagina torna vuota, o quando non si guadagna piu'
    storia (la fonte non ha altro indietro) — quest'ultimo guard evita loop
    infiniti.
    """
    window = timedelta(seconds=GRANULARITY_SECONDS * MAX_CANDLES_PER_REQUEST)
    target_start = _utcnow() - timedelta(days=years * 365)

    by_time: dict[int, dict] = {}  # open_time(sec) -> candela  (dedup naturale)
    end = _utcnow()
    previous_oldest: int | None = None
    pages = 0

    while True:
        start = end - window
        rows = _fetch_window(product, start, end)
        pages += 1
        if not rows:
            break

        for row in rows:
            ts = int(row[0])
            by_time[ts] = {
                "open_time_ms": ts * 1000,
                "open": float(row[3]),
                "high": float(row[2]),
                "low": float(row[1]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }

        oldest = min(int(row[0]) for row in rows)
        # Guard anti-loop: se la barra piu' vecchia non arretra piu', la fonte non
        # ha altra storia disponibile per questo prodotto: fermati.
        if previous_oldest is not None and oldest >= previous_oldest:
            break
        previous_oldest = oldest

        # Abbiamo raggiunto (o superato) la profondita' richiesta?
        if datetime.fromtimestamp(oldest, tz=timezone.utc) <= target_start:
            break

        # La prossima pagina finisce dove questa e' iniziata (barra di confine
        # inclusa: il dedup la assorbe).
        end = datetime.fromtimestamp(oldest, tz=timezone.utc)
        time.sleep(REQUEST_PAUSE_SECONDS)

    # Ordina in cronologico e taglia esattamente alla finestra richiesta.
    candles = [by_time[k] for k in sorted(by_time)]
    target_ms = int(target_start.timestamp() * 1000)
    candles = [c for c in candles if c["open_time_ms"] >= target_ms]

    # Scarta l'ultima candela se l'ora non e' ancora chiusa: per il training
    # vogliamo solo barre concluse (stessa filosofia is_final del bot).
    now_ms = int(_utcnow().timestamp() * 1000)
    if candles and candles[-1]["open_time_ms"] + GRANULARITY_SECONDS * 1000 > now_ms:
        candles.pop()

    return candles


def detect_gaps(candles: list[dict]) -> list[tuple[int, int]]:
    """Ritorna i buchi (barre orarie mancanti) come coppie (prev_ms, next_ms).

    Non li riempiamo qui: e' una diagnostica. Sara' il passo di feature a decidere
    come trattarli. Un mercato liquido come BTC/ETH dovrebbe averne pochissimi;
    tanti buchi sono un segnale che i dati vanno guardati prima di fidarsene.
    """
    step_ms = GRANULARITY_SECONDS * 1000
    gaps = []
    for prev, nxt in zip(candles, candles[1:]):
        if nxt["open_time_ms"] - prev["open_time_ms"] != step_ms:
            gaps.append((prev["open_time_ms"], nxt["open_time_ms"]))
    return gaps


def write_csv(path: Path, candles: list[dict]) -> None:
    """Scrive il CSV con timestamp sia in millisecondi (per il codice) sia in ISO
    UTC leggibile (per un controllo a occhio)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["open_time_ms", "open_time_iso", "open", "high", "low", "close", "volume"]
        )
        for c in candles:
            iso = datetime.fromtimestamp(
                c["open_time_ms"] / 1000, tz=timezone.utc
            ).isoformat()
            writer.writerow(
                [c["open_time_ms"], iso, c["open"], c["high"], c["low"],
                 c["close"], c["volume"]]
            )


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scarica lo storico 1h di BTC/ETH/SOL da Coinbase (training)"
    )
    parser.add_argument("--years", type=float, default=2.0,
                        help="anni di storico da scaricare (default 2)")
    parser.add_argument("--assets", nargs="+", default=list(PRODUCTS),
                        choices=list(PRODUCTS),
                        help="asset da scaricare (default: tutti)")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR,
                        help="cartella di output dei CSV")
    args = parser.parse_args(argv)

    print(f"Storico 1h · {args.years} anni · fonte Coinbase Exchange\n")
    exit_code = 0
    for asset in args.assets:
        product = PRODUCTS[asset]
        print(f"[{asset}] scarico {product} ...", flush=True)
        try:
            candles = fetch_history(product, years=args.years)
        except RuntimeError as exc:
            print(f"[{asset}] ERRORE: {exc}\n", flush=True)
            exit_code = 1
            continue

        if not candles:
            print(f"[{asset}] nessuna candela ricevuta\n", flush=True)
            exit_code = 1
            continue

        gaps = detect_gaps(candles)
        out = args.outdir / f"{asset}_1h.csv"
        write_csv(out, candles)

        first = _fmt(candles[0]["open_time_ms"])
        last = _fmt(candles[-1]["open_time_ms"])
        # Copertura: quante barre abbiamo vs quante ne attenderemmo senza buchi.
        span_hours = (
            candles[-1]["open_time_ms"] - candles[0]["open_time_ms"]
        ) // (GRANULARITY_SECONDS * 1000) + 1
        coverage = len(candles) / span_hours * 100 if span_hours else 0.0
        print(
            f"[{asset}] {len(candles)} barre · dal {first} al {last} UTC · "
            f"buchi: {len(gaps)} · copertura {coverage:.2f}% · -> {out}\n",
            flush=True,
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
