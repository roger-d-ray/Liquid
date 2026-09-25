# Decisioni di progetto — regime detector

Registro delle decisioni **chiuse**, con la motivazione e l'evidenza che le ha
prodotte. Serve a evitare che vengano riaperte da zero fra sei mesi — da una
persona o da una sessione Claude futura.

> **Regola di manutenzione.** Il retraining periodico **ricalcola i parametri**
> sui dati nuovi. **Non** rimette in discussione il numero di stati, le feature,
> la fonte dati né le soglie. Quelle sono decisioni di architettura, prese qui.
> Riaprirle richiede evidenza **nuova e del tipo indicato** in fondo a ciascuna
> sezione — non una nuova esecuzione del training.

---

## 0. Regole di sistema — valgono per QUALSIASI filtro, presente o futuro

### 0.1 Un filtro serve ad APRIRE, mai a PROTEGGERE

Un filtro (il regime detector, o qualunque filtro aggiunto in futuro) può solo
**impedire l'apertura di nuove posizioni**. Non blocca, non ritarda e non
condiziona mai la machinery di protezione dello **STEP 0**: flatten intraday,
`manage_positions.py`, modifica degli stop, chiusure.

Se un filtro fallisce, la conseguenza ammessa è una sola: **nessun nuovo trade
in quel ciclo**, con notifica Telegram. Mai "posizioni aperte senza gestione".
Motivo: un 403 di una fonte dati non deve lasciare una posizione a 20x senza
stop management. Ogni filtro futuro va progettato con questa separazione, e il
suo fallimento dev'essere testabile indipendentemente dallo STEP 0.

### 0.2 Fail-closed: un filtro che non può decidere tace

Dati insufficienti, fonte irraggiungibile, storico non contiguo, hash non
combaciante, file mancante: il filtro **non emette nulla**. Non emette "un
valore con confidence bassa", non stima, non usa l'ultimo valore noto.

### 0.3 Metodo: "quale feature separa questi cluster" si misura con l'effect size

La separazione di un cluster dagli altri, feature per feature, si misura con

    d² = Δ² / varianza_pooled      (Δ = differenza delle medie standardizzate)

e **mai** con la sola distanza fra centroidi. La versione a sole medie ignora
la dispersione **dentro** ciascuno stato: una feature con grande spostamento
del centroide ma varianza enorme sembra dominante pur discriminando poco.
Caso reale (§1): sul terzo stato di BTC la metrica a sole medie indicava
`volume_ratio` al 60%; con le varianze la dominante è `adx` (43%, volume 42%).
Una conclusione già consegnata come buona ("volume anomalo con direzionalità
bassa") era un artefatto della metrica. La lezione vale per ogni analisi di
cluster futura, non solo per questa.

---

## 1. Due stati (range / trend), non tre

**Decisione:** il detector emette due stati, uniformi su BTC/ETH/SOL.

### Perché il BIC non decide — da richiamare ogni volta che si ripropone "ma il BIC dice 3"

> **La log-likelihood misura la densità delle feature, non la separazione della
> dimensione decisionale.**

Il 3-stati vinceva tutto il lato statistico: ΔBIC ≈ 11.000 contro una penalità
di ~127; walk-forward held-out +0,29/+0,32 nat/barra, segno positivo in **24
fold su 24**, fold peggiore ancora 3,7× la soglia pre-registrata. È stato
riportato così, senza ammorbidirlo. Non basta, e la ragione **non** è una
penalità troppo blanda: correggendo per l'autocorrelazione (N efficace ~1.200
invece di 16.912) la penalità passa da 127 a ~92 — irrilevante. L'autocorrelazione
non salva i 2 stati.

### La meccanica: rifinitura di densità, non scoperta di regime

Controprova: riaddestramento con **sole ADX + KER**, rimuovendo del tutto ATR% e
volume. Il vantaggio del terzo stato **sopravvive al 93–98%**
(BTC 98,1% · ETH 95,7% · SOL 93,2%). L'ipotesi "volatilità travestita" era
sbagliata, e la realtà è peggiore per il 3-stati:

- l'**ADX è limitato inferiormente da 0 e fortemente asimmetrico** (lunga coda a
  destra, verso i trend forti);
- una miscela di gaussiane approssima male una distribuzione asimmetrica e
  troncata; **una terza gaussiana ne fitta meglio la coda**, sempre;
- quindi la log-likelihood sale **che esista o no un terzo regime di mercato**.
  Il guadagno è rifinitura della forma della densità direzionale.

### Accanto, la prova che il vocabolario uniforme sarebbe saltato comunque

Feature che domina la separazione del terzo stato, con la metrica corretta (§0.3):

| Asset | feature dominante | quote (d² normalizzato) |
|---|---|---|
| BTC | `adx` | adx 43,1 · vol 42,4 · atr 10,3 · ker 4,1 |
| ETH | `volume_ratio` | vol 68,9 · atr 29,4 · adx 1,7 · ker 0,0 |
| SOL | `atr_pct` | atr 59,2 · vol 18,5 · adx 13,9 · ker 8,3 |

**Tre asset, tre concetti diversi.** Il terzo stato non è la stessa cosa fra
asset: anche vincendo ogni test, non darebbe al bot un'etichetta con un
significato unico.

### Il veto: non esiste una terza porta

**Il bot ha due sole skill generatrici di segnale.** Da `CLAUDE.md`:

> *"Skill da privilegiare: **momentum-trading** e **range-trading** (le due
> intraday). **trend-following** è usata solo come filtro di direzione (EMA50/200
> a 1h): non come generatore di segnali intraday."*

La decisione del detector è **strutturalmente binaria**. Le quattro mappature
possibili per un terzo stato falliscono tutte:

| # | Mappatura | Perché fallisce |
|---|---|---|
| 1 | "Non operare" | Costa il 23–38% delle occasioni. Andrebbe giustificato con evidenza di **PnL** negativo in quello stato; la log-likelihood non la fornisce. |
| 2 | Assorbito in momentum | Non è uno stato decisionale distinto: cosmetica. |
| 3 | Assorbito in range | Idem, e contraddice i centroidi (direzionalità intermedia, non bassa). |
| 4 | "Entrambe, con confidence più alta" | Reintroduce il giudizio discrezionale che il detector esiste per rimuovere. |

### È anche il più instabile proprio dove conta: sotto filtro forward

| | disaccordo online↔Viterbi | churn | episodio "transition" online | errore quando lo afferma |
|---|---|---|---|---|
| 2 stati | 5,8–6,2% | 4,5–6,5% | — | — |
| 3 stati | 8,9–9,1% | 6,5–11,1% | 6,1h (BTC) | 12–15% |

### Regola generale

> **Nel nostro sistema la volatilità informa la SIZE, non la scelta della STRATEGIA.**

TP/SL su ATR 15m, `risk_pct × equity`, tetto di leva, `manage_positions.py`
gestiscono già la volatilità. Uno stato di regime che la codifica è ridondante.

### Cosa riaprirebbe la decisione

Solo un **backtest di PnL** che mostri, in modo statisticamente solido, che
operare dentro un terzo stato perde denaro. Non un nuovo BIC, non un nuovo
walk-forward.

---

## 2. Un modello per asset, non condiviso

Feature unitless o normalizzate ma **non identicamente distribuite** (ATR%
mediano BTC 0,6% · ETH 0,9% · SOL 1,1%): un modello unico rischiava di imparare
l'identità dell'asset invece del regime. I dati per-asset bastano (~17.000
osservazioni per poche decine di parametri); i tre asset sono correlati, quindi
unirli non aggiungerebbe informazione indipendente.

---

## 3. Fonte dati in live: Coinbase, la stessa del training

Misurato con `compare_sources.py` su **522 ore sovrapposte, un solo periodo**
(settembre 2026):

| | BTC | ETH | SOL |
|---|---|---|---|
| scarto close (mediana) | 0,63 bps | 0,69 bps | 0,98 bps |
| `volume_ratio`, scarto mediano in σ | 0,26 | 0,22 | 0,35 |
| **disaccordo di regime Coinbase↔Kraken** | **5,75%** | 2,87% | 3,83% |

Prezzi coincidenti, ma il volume è **specifico del venue** (Kraken muove il
29–39% del volume Coinbase) e `volume_ratio` non trasferisce. Un disaccordo
fino al 5,75% delle ore è dello stesso ordine dell'intero scarto
online↔retrospettivo: non trascurabile. **Kraken non è un fallback accettabile
per il regime.** Riaddestrare su Kraken non è un'alternativa: il suo endpoint
OHLC dà ~720 barre e non pagina all'indietro, non può fornire lo storico.

**`compare_sources.py` va rieseguito a ogni retraining** (§8): la conclusione è
netta ma poggia su un solo periodo. Seconda esecuzione (25/09/2026, al primo
retraining, con il modello esportato): disaccordo BTC 5,37% · ETH 2,69% ·
SOL 4,61% — coerente. **Non è però un periodo indipendente**: le due finestre di
~30 giorni si sovrappongono per ~28. Conferma la conclusione con il nuovo
modello, non su dati nuovi; l'indipendenza arriverà dai retraining successivi.

**Semantica dell'endpoint — misurata, non assunta.** `/candles` con
`[start, end]` include **entrambi** gli estremi: `[a, a+299h]` → 300 candele.
Pagine `[a, a+299h]` e `[a−300h, a−1h]` sono quindi disgiunte e adiacenti per
costruzione. (`[a, a+300h]` ha restituito 301 candele: il tetto documentato di
300 non è applicato rigidamente, e non ci si conta.)

**Carico in live: 1 richiesta per asset per run** (300 barre = 101 righe di
feature, 3,3× il punto di convergenza misurato). Più storico peggiorerebbe la
disponibilità (§4). Totale: 3 richieste all'ora. Il limite pubblico Coinbase è,
per quanto noto, dell'ordine di 10 richieste/s per IP — **non verificato in
sessione** (il dominio della documentazione era bloccato dal proxy); il margine
è di vari ordini di grandezza anche se quel valore fosse sbagliato di 10×. Se
lo si toccasse: HTTP 429 → retry con backoff 1s/2s/4s → poi `SourceUnavailable`
→ nessun regime in quel ciclo (§0.1, §0.2). Al massimo 4 tentativi per
richiesta: nessuna tempesta di retry. Il limite non si misura martellando l'API:
si rischierebbe il blocco dell'IP cloud.

---

## 4. Fail-safe — soglie misurate, non stimate

| Vincolo | Valore | Evidenza |
|---|---|---|
| Barre per finestra feature | **200** contigue | ADX convergente a 200 (errore ~0 contro riferimento a 800; ~6,5 a 30 barre) |
| Righe di feature per il filtraggio | **≥ 50** | vedi sotto |
| Barre contigue minime | **249** | 200 + 50 − 1 |
| Barre richieste in live | **300** | una pagina Coinbase |

**Il minimo di 50 righe è stato misurato sul modello a 2 stati.** Convergenza
della decisione filtrata all'ultima barra (accordo di stato 100% e |Δconfidence|
≤ 1e-4 rispetto alla storia piena):

| Modello | BTC | ETH | SOL | margine richiesto 1,5× |
|---|---|---|---|---|
| validato (step 4) | 15 | 15 | 30 | 45 ≤ 50 |
| esportato 25/09/2026 | 20 | 20 | 15 | 30 ≤ 50 |

**La soglia non viene data per scontata dopo un retraining:** `train_model.py`
rimisura la convergenza di ogni nuovo modello e `export_model.py` **rifiuta** se
`1,5 × convergenza > 50` (verificato: un modello a 40 righe viene rifiutato). Il
punto di convergenza oscilla fra retraining (15–30 righe) e l'asset più lento
cambia: il margine va letto sul caso peggiore, oggi 45 su 50. Se un modello
futuro converge più lentamente, l'export si ferma e la soglia va rivista qui —
non forzata.

**Regola di contiguità.** Il detector usa la coda contigua che termina
all'ultima barra chiusa; se è più corta di 249 barre, tace. Un buco della fonte
**più vecchio** della coda necessaria non conta: nessuna finestra di feature lo
attraversa, esattamente come nel training. Un buco **esattamente su una
giunzione fra pagine** invece fa rifiutare comunque: lì un buco della fonte e un
errore di cucitura sono indistinguibili. (Con 1 pagina in live, non ci sono
giunzioni.)

**Conseguenza nota, da accettare consapevolmente:** dopo un buco della fonte, il
detector tace finché non si accumulano 249 barre contigue — **fino a ~10,4
giorni**. Nello storico di 2 anni: 2 buchi (manutenzioni Coinbase, 5 ore,
simultanei sui tre asset) → ~21 giorni senza regime, ~2,8% del tempo, cioè ~2,8%
del tempo senza nuovi trade. Il costo scende solo accorciando la finestra
feature, che richiede di rifare la validazione.

---

## 5. Integrità train/serve — cosa è verificato, e quando

Tre moduli a **sorgente unica**, scritti solo in `regime-training/` e copiati
verbatim nel bot da `export_model.py`:

| Modulo | Hash registrato nel momento dell'uso | Verificato |
|---|---|---|
| `regime_features.py` | al **build del dataset** (manifest) | export + runtime |
| `regime_hmm.py` | alla **verifica di parità** (training) | export + runtime |
| `regime_source.py` | al **fetch dello storico** (provenienza) | export + runtime |

**Perché "nel momento dell'uso".** Il primo design registrava l'hash
all'export. Buco: modificando `regime_features.py` dopo il build e prima
dell'export, l'export avrebbe registrato l'hash del file nuovo accanto a un
modello addestrato con il vecchio — skew silenzioso, accettato dal detector.
Ora l'hash viaggia dentro l'artefatto dal momento in cui il codice viene usato,
e l'export verifica che il file attuale coincida.

Inoltre:
- `models/regime_model.meta.json` registra l'hash di **ogni file modello**: file
  mancante o alterato → nessun regime;
- registra **fonte (`coinbase-exchange`) e granularità (3600s)**: il detector le
  confronta con quelle dichiarate da `regime_source.py`; cambiare fonte senza
  riaddestrare → nessun regime;
- `.gitattributes` marca questi file `-text`: git non ne normalizza mai i byte
  (fine riga), altrimenti un checkout altrove cambierebbe l'hash.

Modello di minaccia: **divergenza accidentale**, non manomissione deliberata (chi
modifica insieme un modello e i suoi metadati può aggirare il controllo).

---

## 6. Etichettatura derivata dai centroidi, mai assegnata a mano

hmmlearn numera gli stati in modo arbitrario — già oggi: su BTC "trend" è lo
stato 0, su ETH e SOL è lo stato 1. La regola (in `regime_hmm.LABEL_RULE`,
registrata nei metadati):

> **trend** = lo stato con media più alta **sia** di ADX **sia** di Kaufman ER;
> **range** = l'altro. Se ADX e KER indicano stati diversi, **nessuna
> etichetta**: il modello non corrisponde alla semantica validata e va rivisto
> da una persona, non indovinato.

Derivata in training, **ri-derivata e confrontata** all'export, e ri-derivata
dal detector a runtime. Un retraining che permuta gli stati resta etichettato
correttamente da solo.

---

## 7. Il modello è versionato in git

Il container cloud si riclona da zero a ogni run: ciò che resta git-ignored
sparisce. `export_model.py` è l'unico passaggio da `regime-training/artifacts/`
(git-ignored) a `models/` e ai tre moduli nella root del bot, **tutti
committati**. Restano git-ignored solo lo storico e gli artefatti intermedi.

---

## 8. Processo di retraining (anteprima dello step 8)

1. `fetch_history.py` — storico + provenienza
2. `build_dataset.py` — feature + manifest (hash del codice feature)
3. `train_model.py` — 2 stati; parità, etichette e convergenza **bloccanti**
4. `compare_sources.py` — riconferma della decisione di fonte (§3)
5. `export_model.py` — tutte le verifiche; rifiuta invece di avvisare
6. `python3 -m unittest discover -s regime-training -p 'test_*.py'`
   (con `REGIME_LIVE_TESTS=1` anche la cucitura contro Coinbase reale)
7. **commit** di `models/` e dei tre moduli

Il retraining **non** cambia numero di stati, feature, fonte, soglie.
