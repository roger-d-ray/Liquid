"""regime_source.py — fonte dati del regime detector: Coinbase Exchange, barre 1h.

SORGENTE UNICA, come regime_features.py e regime_hmm.py. Usato dal training
(fetch_history.py scarica lo storico con questo codice) e copiato VERBATIM nel
bot da export_model.py, dove il detector live lo usa per il fetch. Il suo SHA-256
viene registrato al momento del fetch di training e verificato all'export e in
produzione: se la copia nel bot diverge, il detector tace.

Perche' Coinbase anche in live
------------------------------
Il modello e' addestrato su candele Coinbase. Un'altra fonte reintroduce
train/serve skew dalla porta dei dati: con Kraken il regime diverge fino al 5,75%
delle ore, perche' il volume e' specifico del venue (DECISIONS.md §3).

Semantica Coinbase — MISURATA, non assunta
------------------------------------------
/products/{id}/candles con [start, end] include ENTRAMBI gli estremi:
[a, a+299h] restituisce esattamente 300 candele, con a e b presenti. Le pagine
[a, a+299h] e [a-300h, a-1h] sono quindi disgiunte e adiacenti per costruzione:
nessun margine, nessuna deduplicazione, nessuna barra saltata alla giunzione.

Contratto fail-closed
---------------------
Ogni funzione di fetch restituisce una serie COMPLETA E VERIFICATA oppure solleva
un SourceError. Non restituisce mai una serie parziale o mutilata.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── Identita' della fonte: registrata nei metadati del modello ───────────────
SOURCE_ID = "coinbase-exchange"
GRANULARITY_SECONDS = 3600

COINBASE_BASE = "https://api.exchange.coinbase.com"
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}

PAGE_BARS = 300               # candele per richiesta ([start,end] inclusivi)
CLOSE_GRACE_SECONDS = 60      # una barra e' "chiusa" solo 60s dopo la sua fine
MAX_RETRIES = 3               # tentativi extra su errori transitori
REQUEST_PAUSE_SECONDS = 0.25  # pausa fra pagine consecutive
HTTP_TIMEOUT_SECONDS = 20


# ── Errori: una radice sola, cosi' il chiamante ha un solo punto di rifiuto ──
class SourceError(RuntimeError):
    """La fonte non ha fornito una serie affidabile. Nessun regime."""


class SourceUnavailable(SourceError):
    """Rete o HTTP: nessuna risposta utile dopo i retry, o rifiuto esplicito."""


class StitchError(SourceError):
    """Le pagine non si ricuciono in modo verificabile."""


class InsufficientHistory(SourceError):
    """Lo storico contiguo e' piu' corto del minimo richiesto."""


class StaleData(SourceError):
    """Manca l'ultima barra chiusa attesa: il dato non e' fresco."""


class InvalidCandle(SourceError):
    """Riga della fonte malformata o incoerente."""


class HttpStatusError(Exception):
    """Errore HTTP con status esplicito (normalizza urllib, rende i test semplici)."""

    def __init__(self, status: int, reason: str = ""):
        super().__init__(f"HTTP {status} {reason}".strip())
        self.status = status


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def default_http_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "liquid-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code, str(exc.reason)) from exc


# ── Parser: l'UNICO punto in cui una riga Coinbase diventa una candela ───────
def parse_candle_row(row) -> dict:
    """[time, low, high, open, close, volume] -> candela.

    Training e live passano entrambi da qui: stessa riga, stessa interpretazione.
    Righe incoerenti vengono rifiutate, mai corrette o inventate.
    """
    if not isinstance(row, (list, tuple)) or len(row) != 6:
        raise InvalidCandle(f"riga malformata: {row!r}")
    try:
        t = int(row[0])
        low, high, open_, close, volume = (float(row[i]) for i in (1, 2, 3, 4, 5))
    except (TypeError, ValueError) as exc:
        raise InvalidCandle(f"valori non numerici: {row!r}") from exc
    values = (low, high, open_, close, volume)
    if not all(math.isfinite(v) for v in values):
        raise InvalidCandle(f"valori non finiti: {row!r}")
    if t % GRANULARITY_SECONDS:
        raise InvalidCandle(f"timestamp non allineato all'ora: {t}")
    if not (volume >= 0 and 0 < low <= min(open_, close) and high >= max(open_, close)):
        raise InvalidCandle(f"OHLCV incoerente: {row!r}")
    return {"open_time_ms": t * 1000, "open": open_, "high": high,
            "low": low, "close": close, "volume": volume}


# ── Pianificazione pagine ────────────────────────────────────────────────────
def last_closed_open_time(now_s: float, grace: int = CLOSE_GRACE_SECONDS) -> int:
    """open_time (s) dell'ultima barra la cui chiusura + grace e' <= now."""
    g = GRANULARITY_SECONDS
    return ((int(now_s) - grace) // g) * g - g


def plan_pages(end_open: int, n_bars: int) -> list[tuple[int, int]]:
    """Pagine (a, b) inclusive, dalla piu' recente alla piu' vecchia.

    Invarianti (verificate dai test): ogni pagina ha <= PAGE_BARS barre; pagine
    consecutive sono ADIACENTI (b_vecchia + 1h == a_nuova) e DISGIUNTE; l'unione
    copre esattamente n_bars barre.
    """
    if n_bars <= 0:
        raise ValueError("n_bars deve essere positivo")
    g = GRANULARITY_SECONDS
    pages, b, remaining = [], end_open, n_bars
    while remaining:
        k = min(PAGE_BARS, remaining)
        a = b - (k - 1) * g
        pages.append((a, b))
        remaining -= k
        b = a - g
    return pages


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, HttpStatusError):
        return exc.status == 429 or 500 <= exc.status < 600
    # urllib.error.URLError e' un OSError; JSONDecodeError e' un ValueError
    return isinstance(exc, (OSError, TimeoutError, ValueError))


# ── Una pagina: retry sui soli errori transitori ─────────────────────────────
def fetch_page(product: str, a: int, b: int, *, http_get=default_http_get,
               sleep=time.sleep, max_retries: int = MAX_RETRIES) -> dict[int, dict]:
    """Barre con open_time in [a, b]. Ammette buchi della fonte; non ammette
    barre fuori pagina ne' duplicati. Errore non transitorio -> rifiuto immediato;
    transitorio -> backoff 1s, 2s, 4s; esauriti i tentativi -> rifiuto."""
    q = urllib.parse.urlencode({"granularity": GRANULARITY_SECONDS,
                                "start": _iso(a), "end": _iso(b)})
    url = f"{COINBASE_BASE}/products/{product}/candles?{q}"
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            payload = http_get(url)
            break
        except Exception as exc:  # noqa: BLE001 - classificato subito sotto
            if not _is_transient(exc):
                raise SourceUnavailable(
                    f"{product} [{_iso(a)} .. {_iso(b)}]: {exc}") from exc
            last_exc = exc
            if attempt < max_retries:
                sleep(2 ** attempt)
    else:
        raise SourceUnavailable(
            f"{product} [{_iso(a)} .. {_iso(b)}]: fallita dopo "
            f"{max_retries + 1} tentativi ({last_exc})") from last_exc

    if not isinstance(payload, list):
        raise SourceUnavailable(f"{product}: risposta inattesa {str(payload)[:200]}")
    bars: dict[int, dict] = {}
    for row in payload:
        bar = parse_candle_row(row)
        t = bar["open_time_ms"] // 1000
        if not a <= t <= b:
            raise StitchError(f"{product}: barra {_iso(t)} fuori dalla pagina "
                              f"[{_iso(a)} .. {_iso(b)}]")
        if t in bars:
            raise StitchError(f"{product}: timestamp duplicato {_iso(t)} nella pagina")
        bars[t] = bar
    return bars


# ── Un intervallo: piu' pagine, ricucite e verificate ────────────────────────
def fetch_range(asset: str, first_open: int, last_open: int, *,
                http_get=default_http_get, sleep=time.sleep,
                max_retries: int = MAX_RETRIES):
    """Tutte le barre con open_time in [first_open, last_open].

    Ritorna (barre per open_time, pagine usate). I buchi della FONTE restano
    buchi (lo storico di training li registra); una pagina fallita invece fa
    fallire tutto: non esiste un ritorno parziale.
    """
    if asset not in PRODUCTS:
        raise SourceError(f"asset non supportato: {asset}")
    g = GRANULARITY_SECONDS
    if last_open < first_open or (last_open - first_open) % g:
        raise ValueError("intervallo non valido")
    pages = plan_pages(last_open, (last_open - first_open) // g + 1)
    collected: dict[int, dict] = {}
    for i, (a, b) in enumerate(pages):
        if i:
            sleep(REQUEST_PAUSE_SECONDS)
        page = fetch_page(PRODUCTS[asset], a, b, http_get=http_get,
                          sleep=sleep, max_retries=max_retries)
        overlap = collected.keys() & page.keys()
        if overlap:  # impossibile per costruzione: se accade, qualcosa e' cambiato
            raise StitchError(f"{asset}: sovrapposizione fra pagine su "
                              f"{_iso(min(overlap))}")
        collected.update(page)
    return collected, pages


# ── Il fetch del detector live ───────────────────────────────────────────────
def fetch_recent_bars(asset: str, *, min_bars: int, target_bars: int = PAGE_BARS,
                      now: float | None = None, http_get=default_http_get,
                      sleep=time.sleep, max_retries: int = MAX_RETRIES) -> list[dict]:
    """Le ultime barre 1h CHIUSE, contigue, verificate. Oppure SourceError.

    Controlli, in ordine:
    1. ogni pagina riuscita (altrimenti SourceUnavailable: mai serie parziale);
    2. nessuna sovrapposizione e nessuna barra fuori pagina (StitchError);
    3. nessun buco ESATTAMENTE su una giunzione fra pagine (StitchError): li'
       un buco della fonte e un errore di cucitura sono indistinguibili, quindi
       si rifiuta;
    4. presente l'ultima barra chiusa attesa (StaleData);
    5. la coda contigua che termina all'ultima barra chiusa e' >= min_bars
       (InsufficientHistory). Un buco della fonte PIU' VECCHIO della coda
       necessaria non conta: nessuna finestra di feature lo attraversa, esattamente
       come nel training (build_dataset scarta le finestre a cavallo dei buchi).
    """
    if not 0 < min_bars <= target_bars:
        raise ValueError("serve 0 < min_bars <= target_bars")
    g = GRANULARITY_SECONDS
    end_open = last_closed_open_time(time.time() if now is None else now)
    first_open = end_open - (target_bars - 1) * g
    collected, pages = fetch_range(asset, first_open, end_open, http_get=http_get,
                                   sleep=sleep, max_retries=max_retries)

    for (a_new, _), (_, b_old) in zip(pages, pages[1:]):
        if b_old + g != a_new:
            raise StitchError(f"{asset}: piano pagine incoerente")
        if a_new not in collected or b_old not in collected:
            raise StitchError(
                f"{asset}: buco sulla giunzione fra pagine "
                f"({_iso(b_old)} | {_iso(a_new)}), indistinguibile da un errore "
                f"di cucitura")

    if end_open not in collected:
        raise StaleData(f"{asset}: manca l'ultima barra chiusa attesa {_iso(end_open)}")

    tail, t = [], end_open
    while t >= first_open and t in collected:
        tail.append(collected[t])
        t -= g
    tail.reverse()
    if len(tail) < min_bars:
        raise InsufficientHistory(
            f"{asset}: {len(tail)} barre contigue, minimo {min_bars}")
    return tail
