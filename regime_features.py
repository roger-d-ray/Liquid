"""regime_features.py — SORGENTE UNICA delle feature di regime (train + inference).

Questo file e' l'unico posto in cui vive la logica che trasforma una finestra di
candele OHLCV nei numeri che descrivono il *regime* di mercato. Viene scritto UNA
sola volta qui, in ``regime-training/``, e al momento dell'export una copia
byte-per-byte finisce nella directory del bot, con il suo hash SHA-256 registrato
nei metadati del modello. Il detector live verifica quell'hash a ogni run: se la
copia diverge, si rifiuta di produrre un regime invece di sbagliare in silenzio.

REGOLA D'ORO: non modificare mai a mano la copia nel bot. Si edita solo questo
file, si ri-addestra e si ri-esporta.

Le 4 feature (una per dimensione statistica, senza ridondanza)
-------------------------------------------------------------
1. ``adx``          — forza/direzionalita' del trend (0-100).  ADX alto = trend
                      marcato; ADX basso = mercato senza direzione (range).
2. ``atr_pct``      — volatilita' relativa al prezzo (ATR / close). Normalizzata
                      cosi' e' confrontabile nel tempo e tra asset.
3. ``kaufman_er``   — Kaufman Efficiency Ratio (0-1): quanto e' "dritto" il
                      movimento. ~1 = percorso efficiente/persistente (trend);
                      ~0 = molto rumore, va e torna (mean-reversion/range).
4. ``volume_ratio`` — volume corrente / media a 20 barre: conferma di
                      partecipazione. E' l'unica dimensione non coperta dalle
                      altre tre.

Riuso delle formule (NON riscritte da zero)
-------------------------------------------
ADX, ATR e volume ratio riprendono la matematica gia' in produzione in
``data_fetcher.py`` (funzioni ``adx``, ``atr``, ``sma``, ``vol_ratio``). La
Kaufman ER riprende ``efficiency_ratio`` (periodo 20) gia' validata e in uso in
``skills/range-trading/scripts/compute_range_metrics.py``. Le formule sono
*copiate* (non importate) di proposito, per tenere questo file autocontenuto e
per congelarle insieme al modello. Nessuna dipendenza esterna: solo standard lib.
"""

from __future__ import annotations

# ── Costanti (fanno parte del file hashato: cambiano il regime se toccate) ────
FEATURE_NAMES: tuple[str, ...] = ("adx", "atr_pct", "kaufman_er", "volume_ratio")

ADX_PERIOD = 14          # Wilder, come in data_fetcher.adx
ATR_PERIOD = 14          # Wilder, come in data_fetcher.atr
KER_PERIOD = 20          # come efficiency_ratio() della skill range-trading
VOLUME_SMA_PERIOD = 20   # media volume, come data_fetcher (vol_sma20)

# Finestra "canonica" di barre su cui calcolare le feature. E' il contratto di
# parita' train/serve: sia build_dataset (training) sia regime_detector (live)
# passano ESATTAMENTE questo numero di barre finali, cosi' ADX/ATR — che usano lo
# smoothing ricorsivo di Wilder e quindi dipendono da quanta storia vedono —
# restituiscono lo stesso valore in entrambi i contesti. In live il bot ha 200
# barre 1h disponibili, quindi 200 e' un numero coerente con la produzione.
FEATURE_WINDOW_BARS = 200

# Minimo assoluto di barre sotto cui non ha senso calcolare (ADX ne richiede
# almeno 2*periodo). Sotto questa soglia compute_features ritorna None.
_MIN_BARS = 2 * ADX_PERIOD


# ── Helper: media semplice (copiata da data_fetcher.sma) ──────────────────────
def _sma(values: list[float], period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ── ATR di Wilder (copiata da data_fetcher.atr) ───────────────────────────────
def _atr(candles: list[dict], period: int = ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    trs = [
        max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
        for i in range(1, len(candles))
    ]
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


# ── ADX di Wilder (copiata da data_fetcher.adx) ───────────────────────────────
def _adx(candles: list[dict], period: int = ADX_PERIOD):
    if len(candles) < period * 2:
        return None
    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        ph, pl = candles[i - 1]["high"], candles[i - 1]["low"]
        pc = candles[i - 1]["close"]
        up, dn = h - ph, pl - l
        plus_dms.append(up if up > dn and up > 0 else 0)
        minus_dms.append(dn if dn > up and dn > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def wilder(s, p):
        v = sum(s[:p])
        r = [v]
        for x in s[p:]:
            v = v - v / p + x
            r.append(v)
        return r

    atr_w = wilder(trs, period)
    pdm_w = wilder(plus_dms, period)
    mdm_w = wilder(minus_dms, period)

    dxs = []
    for a, p, m in zip(atr_w, pdm_w, mdm_w):
        pdi = 100 * p / a if a else 0
        mdi = 100 * m / a if a else 0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)

    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val


# ── Kaufman Efficiency Ratio (copiata da efficiency_ratio() della skill) ──────
def _kaufman_er(closes: list[float], period: int = KER_PERIOD):
    """direction / volatility sull'ultima finestra di ``period`` barre.

    direction  = |close[-1] - close[-1-period]|      (spostamento netto)
    volatility = somma dei |delta| barra-barra        (spostamento totale)
    Ritorna 0.0 se volatility == 0 (mercato piatto = massima inefficienza),
    replicando la logica validata della skill range-trading.
    """
    if len(closes) < period + 1:
        return None
    direction = abs(closes[-1] - closes[-1 - period])
    volatility = sum(
        abs(closes[i] - closes[i - 1])
        for i in range(len(closes) - period, len(closes))
    )
    return 0.0 if volatility == 0 else direction / volatility


# ── Funzione pubblica: le 4 feature per l'ULTIMA barra della finestra ─────────
def compute_features(candles: list[dict]) -> dict | None:
    """Calcola le 4 feature di regime per l'ultima barra di ``candles``.

    ``candles``: lista cronologica (vecchia->recente) di dict con chiavi float
    ``open``/``high``/``low``/``close``/``volume``. Per la parita' train/serve va
    passata la finestra canonica di ``FEATURE_WINDOW_BARS`` barre finali.

    Ritorna ``{feature: valore}`` per tutte e 4 le feature, oppure ``None`` se i
    dati sono insufficienti o una feature non e' calcolabile (il chiamante salta
    quella barra). ``None`` invece di un numero inventato: coerente con la
    filosofia "mai dati finti" del bot.
    """
    if not isinstance(candles, list) or len(candles) < _MIN_BARS:
        return None

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    adx = _adx(candles, ADX_PERIOD)
    atr = _atr(candles, ATR_PERIOD)
    ker = _kaufman_er(closes, KER_PERIOD)
    vol_sma = _sma(volumes, VOLUME_SMA_PERIOD)

    last_close = closes[-1]
    atr_pct = atr / last_close if (atr is not None and last_close) else None
    volume_ratio = (
        volumes[-1] / vol_sma if (vol_sma not in (None, 0)) else None
    )

    values = {
        "adx": adx,
        "atr_pct": atr_pct,
        "kaufman_er": ker,
        "volume_ratio": volume_ratio,
    }
    # Se una qualsiasi feature non e' calcolabile, l'intera riga e' inutilizzabile
    # per l'addestramento: meglio scartarla che passare un buco al modello.
    if any(values[name] is None for name in FEATURE_NAMES):
        return None
    return values
