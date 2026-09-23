# Decisioni di progetto — regime detector

Registro delle decisioni **chiuse**, con la motivazione e l'evidenza che le ha
prodotte. Serve a evitare che vengano riaperte da zero fra sei mesi — da una
persona o da una sessione Claude futura.

> **Regola di manutenzione:** il retraining periodico (step 8) **ricalcola i
> parametri** del modello sui dati nuovi. **Non rimette in discussione il numero
> di stati, la scelta delle feature, né la fonte dati.** Quelle sono decisioni di
> architettura prese qui. Riaprirle richiede evidenza nuova del tipo indicato in
> fondo a ciascuna sezione, non una nuova esecuzione del training.

---

## 1. Due stati (range / trend), non tre

### La decisione
Il detector emette **due** stati, uniformi su BTC/ETH/SOL.

### Perché il BIC non decide — da citare ogni volta che qualcuno ripropone "ma il BIC dice 3"

Il BIC in-sample preferiva 3 stati con un margine enorme (ΔBIC ≈ 11.000 contro una
penalità di soli ~127 punti), e anche il walk-forward held-out dava 3 stati
vincente (+0,29/+0,32 nat/barra, segno positivo in 24 fold su 24).
**Non è stato sufficiente, e la ragione va capita bene:**

> **La log-likelihood misura la densità delle feature, non la separazione della
> dimensione decisionale.**

Non è una questione di penalità troppo blanda. È stato verificato: correggendo per
l'autocorrelazione (persistenza 0,86–0,97 → N efficace ~1.200 invece di 16.912) la
penalità passerebbe da 127 a ~92, irrilevante davanti a 11.000. L'autocorrelazione
**non** salva i 2 stati. Il punto è un altro: una terza gaussiana migliora *sempre*
il fit di una distribuzione asimmetrica — l'ADX è limitato a sinistra da 0 e ha una
lunga coda destra — che esista o meno un terzo regime di mercato. Il guadagno era
fitting di densità, non scoperta di regime.

**Controprova eseguita** (riaddestramento con sole ADX + KER, togliendo del tutto
ATR% e volume): il vantaggio del terzo stato **non crolla**, sopravvive al 93–98%.
Quindi il guadagno non veniva dalla volatilità: veniva dalla forma della densità
direzionale. È la prova diretta che la verosimiglianza non stava separando un
regime, stava modellando meglio una distribuzione.

### Il veto vero: non esiste una terza porta

**Il bot ha due sole skill generatrici di segnale**, non tre. Da `CLAUDE.md`:

> *"Skill da privilegiare: **momentum-trading** e **range-trading** (le due
> intraday). **trend-following** è usata solo come filtro di direzione (EMA50/200
> a 1h): non come generatore di segnali intraday."*

La decisione che il detector deve risolvere è quindi **strutturalmente binaria**:
range → skill range-trading; direzionale → skill momentum-trading. Le quattro
mappature possibili per un terzo stato falliscono tutte:

| # | Mappatura del terzo stato | Perché fallisce |
|---|---|---|
| 1 | **"Non operare"** | Costa il 23–38% delle occasioni operative. Per giustificarlo servirebbe evidenza che *operare* in quello stato **perde denaro** — cioè evidenza di **PnL**. La log-likelihood non la fornisce e non è stata misurata. |
| 2 | **Assorbito in momentum** | Se agisce come "trend", non è uno stato decisionale distinto: è cosmetica che non cambia alcuna azione. |
| 3 | **Assorbito in range** | Idem, e contraddice i centroidi (direzionalità intermedia, non bassa). |
| 4 | **"Entrambe, con soglia di confidence più alta"** | Reintroduce esattamente il giudizio discrezionale che il detector esiste per rimuovere. |

### Due aggravanti emerse dai test

**(a) Il terzo stato non è lo stesso concetto fra asset.** Misurando quale feature
domina la separazione del terzo stato — con una metrica che tiene conto delle
varianze di stato, non solo delle medie — il risultato diverge:

| Asset | feature dominante | quota |
|---|---|---|
| BTC | `adx` (volume quasi pari) | 43% (vol 42%) |
| ETH | `volume_ratio` | 69% |
| SOL | `atr_pct` | 59% |

Tre asset, tre concetti diversi. Il "vocabolario uniforme dei regimi" che
giustificava un numero di stati unico **salterebbe comunque**.

**(b) È il più instabile sotto filtro forward** — cioè proprio in live, dove il bot
non ha il futuro a disposizione:

| | disaccordo online↔Viterbi | churn | episodio "transition" online | errore quando lo afferma |
|---|---|---|---|---|
| 2 stati | 5,8–6,2% | 4,5–6,5% | — | — |
| 3 stati | 8,9–9,1% | 6,5–**11,1%** | **6,1h** (BTC) | **12–15%** |

Su BTC gli episodi durano 6,1 ore, quanto `MAX_HOLD_HOURS=6`: il regime scadrebbe
insieme al trade. E "transition" assorbe il 43–52% di tutto il disaccordo pur
occupando il 28–38% del tempo.

### La regola generale che ne discende

> **Nel nostro sistema la volatilità informa la SIZE, non la scelta della
> STRATEGIA.**

TP/SL su ATR 15m, `risk_pct × equity`, tetto di leva, `manage_positions.py`: la
volatilità è già gestita, e meglio, da machinery esistente. Uno stato di regime che
codifica volatilità è ridondante e non risponde alla domanda per cui il detector
esiste ("quale delle due skill applico?").

### Cosa riaprirebbe la decisione
Solo un **backtest di PnL** che dimostri che operare dentro il terzo stato perde
denaro in modo statisticamente solido. Non un nuovo BIC, non un nuovo walk-forward.

---

## 2. Un modello per asset, non condiviso

Feature unitless o normalizzate, ma **non identicamente distribuite** fra asset
(ATR% mediano: BTC 0,6% · ETH 0,9% · SOL 1,1%). Un modello unico rischiava di
imparare *l'identità dell'asset* invece del regime. I dati per-asset bastano
ampiamente (16.912 osservazioni per poche decine di parametri), e i tre asset sono
molto correlati, quindi unirli non avrebbe aggiunto informazione indipendente.

---

## 3. Fonte dati in live: Coinbase, la stessa del training

Il modello è addestrato su candele **Coinbase**. Usare un'altra fonte in produzione
reintroduce train/serve skew dalla porta dei dati. Misurato su 522 ore sovrapposte:

| | BTC | ETH | SOL |
|---|---|---|---|
| scarto close (mediana) | 0,63 bps | 0,69 bps | 0,98 bps |
| `volume_ratio` scarto mediano (in σ) | **0,26 σ** | **0,22 σ** | **0,35 σ** |
| **disaccordo di regime Coinbase↔Kraken** | **5,75%** | 2,87% | 3,83% |

I prezzi coincidono (sotto 1 bps), ma il **volume è specifico del venue** (Kraken
muove il 29–39% del volume Coinbase) e `volume_ratio` non trasferisce. Il
disaccordo di regime risultante (fino al 5,75% delle ore) è **dello stesso ordine
dell'intero scarto online↔retrospettivo** dei 2 stati: non è trascurabile.

**Decisione: il detector usa Coinbase, paginato** (~3 richieste per asset per run,
qualche secondo — accettabile per una routine oraria). Kraken **non** è un fallback
accettabile per il regime.
*Nota pratica:* riaddestrare su Kraken non è nemmeno un'opzione — il suo endpoint
OHLC restituisce solo ~720 barre e non pagina all'indietro, quindi non può fornire
i 2 anni di storico che il training richiede.

---

## 4. Fail-safe (fail-closed, mai stimare su dati insufficienti)

Misurati sullo storico, non stimati:

| Vincolo | Valore | Evidenza |
|---|---|---|
| Barre per la finestra feature | **200** contigue | ADX pienamente convergente a 200 (errore ~0 vs riferimento 800 barre; ~6,5 a 30 barre) |
| Righe di feature per il filtraggio | **≥50** (minimo duro) | accordo di stato 100% e errore di confidence esattamente 0 già a 30 righe su tutti e tre gli asset; 50 è margine |
| Candele contigue totali richieste | **249** | 200 + 50 − 1 |
| Obiettivo operativo | ~250 righe (≈450 candele) | ampio margine, una manciata di richieste |

**Se uno qualsiasi di questi vincoli non è soddisfatto — fonte irraggiungibile,
barre insufficienti, storico non contiguo, hash del file feature non combaciante —
il detector NON emette un regime.** Non emette "un regime con confidence bassa":
non emette nulla. Conseguenza sul bot: **nessun regime → nessun nuovo trade in quel
ciclo**, con notifica Telegram.

> ⚠️ **Il fallimento del regime non deve MAI bloccare la machinery di sicurezza.**
> STEP 0 (flatten intraday, `manage_positions.py`, modifica SL, chiusure) opera su
> posizioni già aperte e **non dipende dal regime**: deve continuare a girare
> normalmente. Il regime è un filtro per *aprire*, mai per *proteggere*.
