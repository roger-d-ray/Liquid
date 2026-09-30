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
disponibilità (§4). Totale: 3 richieste all'ora.

> ⚠️ **Il limite di rate NON è un fatto verificato.** L'ordine di grandezza
> comunemente citato per l'endpoint pubblico Coinbase è ~10 richieste/s per IP,
> ma **non è stato letto dalla documentazione** (dominio bloccato dal proxy di
> rete) **né misurato** — misurarlo martellando l'API rischierebbe il blocco
> dell'IP cloud. Trattarlo come voce, non come dato: non va citato altrove come
> se fosse verificato.

Non ha comunque importanza pratica: con 3 richieste **all'ora** il margine resta
di vari ordini di grandezza anche se quel numero fosse sbagliato di 100×. Se lo
si toccasse: HTTP 429 → retry con backoff 1s/2s/4s → poi `SourceUnavailable`
→ nessun regime in quel ciclo (§0.1, §0.2). Al massimo 4 tentativi per
richiesta: nessuna tempesta di retry.

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
6. `analyze_confidence.py` — riconferma della soglia di confidence **fuori
   campione** (§10): il ginocchio in walk-forward deve restare in
   **[0,93 – 0,97]**. Se esce dall'intervallo, la soglia si riapre qui — non si
   tiene 0,95 per inerzia.
7. `python3 -m unittest discover -s regime-training -p 'test_*.py'`
   (con `REGIME_LIVE_TESTS=1` anche la cucitura contro Coinbase reale)
8. **commit** di `models/` e dei tre moduli

Il retraining **non** cambia numero di stati, feature, fonte, soglie.

### Indicatore di invecchiamento del modello (da implementare allo step 8)

La **quota di ore con confidence sotto soglia** è un indicatore di invecchiamento:
un modello che descrive sempre peggio il mercato diventa incerto più spesso.
Riferimento misurato fuori campione (walk-forward, §10): BTC 18,1% · ETH 14,7% ·
SOL 15,3%. Si misura in produzione da `logs/regime_runs.jsonl`, dove ogni run
registra per asset `null:below_confidence_threshold`. Se la quota **sale
stabilmente** oltre il riferimento, deve partire un avviso e il retraining va
**anticipato**. Soglia e finestra dell'avviso si fissano allo step 8, prima di
guardare i dati di produzione, con lo stesso metodo pre-registrato usato qui.


---

## 9. Silenzio del detector: visibilità e sorveglianza

### Dove vive il registro — e perché lì

Il container cloud si riclona a ogni run, quindi lo stato non può stare in
`data/` (git-ignored). Il bot ha però **già** il meccanismo giusto: `logs/*.jsonl`
sono **tracciati in git** e `git_push_log.py` li sincronizza, con fallback via
API REST GitHub perché nel cloud il proxy blocca il push su `main`.

Il registro dei silenzi è quindi `logs/regime_silence.jsonl`, append-only, sullo
stesso canale di `proposals.jsonl`. Nessun meccanismo nuovo inventato.

Il canale funziona davvero in produzione, verificato e non assunto: al
30/09/2026 `main` contiene **295 commit `bot: run`** che toccano
`logs/proposals.jsonl`, l'ultimo dello stesso giorno.

`git_push_log.py` accetta ora `--also logs/<nome>.jsonl` per i log aggiuntivi.
Senza argomenti il comportamento è **identico al precedente**: lo dimostra
`test_git_push_log.py`, che esegue la versione nuova e una copia congelata della
vecchia (`test_fixtures/git_push_log_legacy.py`) in 9 scenari — push riuscito,
fallback API con file esistente o assente, niente di nuovo, GET 500, PUT 409,
nessun token, niente da committare, token da `.env` — e confronta **tutti** gli
effetti: comandi git, richieste HTTP (metodo, URL, header, corpo), stdout,
stderr, exit code. Il test è stato a sua volta verificato introducendo 9
deviazioni di comportamento: le coglie tutte.

`--also` accetta **solo** `logs/<nome>.jsonl`: lo script scrive su `main` con un
token, e un percorso sbagliato (es. `.env`) pubblicherebbe segreti.

**Perché non era un dettaglio.** Senza persistenza ogni run ripartirebbe da un
registro vuoto: durante un silenzio di ~10 giorni la logica "notifica solo sulle
transizioni" vedrebbe ogni ora come una transizione (~240 notifiche invece di 2),
e il contatore 5%/90 giorni si azzererebbe a ogni run — l'allarme non
scatterebbe mai, in silenzio.

**Ordine degli eventi.** Il fallback API unisce accodando le righe locali
mancanti su `main`: con run sovrapposti un evento potrebbe finire fuori ordine.
Il registro è quindi ordinato per **timestamp**, non per posizione nel file.

### Il registro osserva, non decide

La decisione fail-closed non dipende mai dal registro. Se il push fallisce e il
registro si perde, il caso peggiore è **una notifica ripetuta**, mai un trade
sbagliato. Il file è letto con tolleranza ai danni: una riga illeggibile viene
saltata invece di far fallire il detector.

### Notifiche: solo sulle transizioni

Una all'inizio del silenzio (con causa, buco della fonte e **data prevista di
ripresa**, stimata dalle barre contigue mancanti) e una alla ripresa. Un silenzio
che continua non notifica nulla: niente messaggi ogni ora.

### Budget di silenzio

Oltre il **5% del tempo negli ultimi 90 giorni** parte un avviso e la questione
della finestra delle feature (§4) si riapre. Una notifica al superamento, una al
rientro. Il 2,8% storico è una misura del passato, non una garanzia.

---

## 10. Soglia di confidence: **0,95**, uniforme sui tre asset

**Decisione:** sotto confidence 0,95 il detector non dichiara un regime operativo
(nessuna nuova apertura). Criterio di conferma fissato **prima** di vedere i
numeri fuori campione: il ginocchio deve cadere in [0,93 – 0,97].

"Errore" = lo stato filtrato online (ciò che il bot vede) differisce da quello
Viterbi retrospettivo. **Ginocchio** = la soglia minima *t* (griglia 0,01) oltre
la quale **ogni** fascia locale larga 0,02 ha errore ≤ 5%; riportato anche con
3% e 10% per mostrare che non dipende da quella scelta.

### Perché non basta la verifica in-sample

La prima tabella usava il modello finale sullo stesso storico su cui era stato
addestrato: il modello confrontato con sé stesso, con una calibrazione della
confidence ottimista per costruzione. La conferma è stata rifatta in
**walk-forward** — stesso protocollo dello step 4 (8 fold expanding × 1.300 ore
held-out), scaler e modello fittati solo sul passato di ciascun fold, una riga
contata solo dopo ≥ 50 righe contigue di filtro (come il detector), e il
riferimento Viterbi esteso oltre la fine del fold, così che a fine fold non
coincida col filtro.

### Risultato: il ginocchio non si sposta

| Asset | criterio 3% | 5% | 10% |
|---|---|---|---|
| BTC | 0,95 / 0,95 | 0,94 / 0,94 | 0,93 / 0,93 |
| ETH | 0,96 / 0,96 | 0,95 / 0,95 | 0,95 / 0,95 |
| SOL | 0,96 / 0,96 | 0,95 / 0,95 | 0,94 / 0,94 |

*(in-sample / walk-forward)* — **coincidono in 9 casi su 9**, tutti in
[0,93 – 0,97]. L'ottimismo in-sample esiste, ma è piccolo e non tocca il
ginocchio: l'errore senza soglia sale di poco fuori campione (BTC 6,07% → 6,55%,
SOL 5,79% → 6,18%) e le fasce di transizione peggiorano un po' (ETH [0,93–0,95)
da 15,8% a 22,1%), ma la confidence oltre la quale l'errore crolla resta la
stessa.

Prezzo in walk-forward (ore escluse → errore sulle ore che restano):

| soglia | BTC | ETH | SOL |
|---|---|---|---|
| nessuna | 0% → 6,55% | 0% → 5,78% | 0% → 6,18% |
| 0,93 | 15,5% → 0,31% | 12,6% → 0,67% | 13,2% → 0,59% |
| **0,95** | **18,1% → 0,05%** | **14,7% → 0,15%** | **15,3% → 0,18%** |
| 0,97 | 22,0% → 0,01% | 17,5% → 0,01% | 18,7% → 0,01% |

### La soglia lavora soprattutto sul gate più debole

A soglia 0,95, walk-forward, per stato che il bot vede:

| | ore escluse range | ore escluse trend | errore range | errore trend |
|---|---|---|---|---|
| BTC | 17,5% | 18,7% | 4,13% → 0,02% | 9,20% → 0,07% |
| ETH | 13,6% | 15,9% | 3,38% → 0,08% | 8,69% → 0,23% |
| SOL | 13,2% | 18,4% | 3,17% → 0,20% | 10,71% → 0,15% |

Il **costo** è quasi simmetrico (il trend perde 1–5 punti di ore in più), il
**beneficio** no: la soglia corregge soprattutto il gate "trend", che senza
soglia era 2,5–3,4× meno affidabile del "range". Dopo la soglia entrambi stanno
sotto lo 0,25%: l'asimmetria in termini assoluti sparisce.

### "Non aprire durante le transizioni di regime" — misurato, non dedotto

Nella prima stesura questa frase era una **deduzione non verificata**. Misura
(walk-forward, finestra ±6 ore da un cambio di stato Viterbi):

| | ore vicine a una transizione | … fra le ore escluse | prob. di esclusione vicino / lontano |
|---|---|---|---|
| BTC | 46,9% | 71,9% | 27,8% / 9,6% |
| ETH | 38,3% | 70,2% | 26,9% / 7,1% |
| SOL | 36,2% | 68,7% | 29,0% / 7,5% |

Vero, ma va detto nella misura giusta: **circa il 70%** delle ore escluse cade
vicino a una transizione, e lì l'esclusione è **3–4× più probabile**. Il
restante ~30% no. La soglia significa "**prevalentemente** non aprire durante
le transizioni di regime", non "solo".

### Avvertenze che restano

> ⚠️ **Questo errore NON è PnL.** Misura quanto spesso l'etichetta di regime
> viene rivista dal senno di poi, non quanto si guadagna. La soglia compra
> **coerenza dell'etichetta**, non redditività (stessa distinzione del §1).

**Costo complessivo:** ~15–18% di ore senza nuove aperture per confidence, più
~2,8% storico di silenzio per buchi della fonte (§4).

**Dove vive la soglia:** nei metadati del modello (`fail_safe`), la applica
Python e non l'agente — il detector espone un verdetto già deciso
(`tradable_regime`), non una confidence da confrontare. Implementazione: step 7.

**Riverifica:** a ogni retraining con `analyze_confidence.py` (§8, passo 6).

---

## 11. Integrazione nel bot (step 7)

### Il verdetto lo decide Python

`regime_detector.py` pubblica per ogni asset **`tradable_regime`**: `"range"` |
`"trend"` | `null`. `null` porta sempre una `reason` (`below_confidence_threshold`,
`insufficient_history`, `stale_data`, `stitch_error`, `source_unavailable`,
`detector_deadline`, `insufficient_feature_rows`, `unlabelled_state`,
`integrity_failed`, `no_model`, `detector_error`, `detector_timeout`,
`detector_crashed`, `detector_invalid_output`, `detector_output_invalid`,
`gate_error`, `gate_unavailable`). `state` e `confidence` restano visibili ma
sono **diagnostici**: l'agente decide solo su `tradable_regime`. La soglia (0,95)
sta nei metadati (`fail_safe.min_confidence`); se manca o è fuori da (0,5 ; 1] è
un errore di integrità — mai un default silenzioso.

### Confidence sotto soglia NON è un silenzio

Lo stato c'è, è solo incerto: capita nel 15–18% delle ore, concentrato sulle
transizioni. Contarlo nel registro dei silenzi vorrebbe dire notificare a ogni
cambio di regime e sforare subito il budget del 5%, che è pensato per i buchi
della fonte (§4). Il registro conta solo i periodi **senza alcuno stato**.

### Il detector non dà la direzione

Le feature non hanno segno (§1): il modello dice *se* c'è trend, non *da che
parte*. La direzione resta alla regola EMA della momentum-trading e al filtro
EMA50/EMA200 a 1h della trend-following.

### Precedenza sulle skill

La classificazione range/trend delle skill (Step 3 della range-trading,
distinzione trend vs `RANGE_OR_CHOP` dello Step 4 della momentum-trading) è
**sostituita** da `tradable_regime` e non si rifà a occhio. Tutto il resto delle
skill resta invariato. È l'unica eccezione alla regola "le skill sono la fonte di
verità", scritta come tale in `CLAUDE.md`. I file delle skill non sono stati
modificati.

### Mai rompere la routine

- `market_summary.py --with-regime` esegue il detector tramite `regime_gate.py`
  in un **processo separato** con timeout duro (25 s) — l'unico modo di avere
  un tetto reale: una risoluzione DNS bloccata non rispetta i timeout dei socket.
  Il detector ha anche un budget interno (20 s), applicato iniettando in
  `regime_source` un trasporto HTTP e uno sleep a scadenza, senza toccare il
  modulo hashato.
- Timeout, crash, output non JSON, exit code inatteso, verdetto incoerente:
  `tradable_regime: null` con `reason`. Il gate **rivalida** l'output: un
  verdetto operativo con confidence sotto la sua soglia viene scartato.
- Nessuna eccezione esce da `run()` del detector né da `get_market_regime()`;
  l'exit code di `market_summary.py` dipende solo dai dati di mercato.
- Tempo misurato end-to-end (rete reale): **~1,1 s** per il summary col regime,
  detector ~1,0 s. Il regime si calcola allo STEP 2, **prima** di
  `show_orderbook`: non consuma la finestra di freschezza di 30 s dell'order book.

### Perché un flag (`--with-regime`) e non il comportamento di default

Senza flag, `market_summary.py` è **identico al byte** alla versione precedente
(test differenziale contro copia congelata): i test esistenti non vanno in rete
e non scrivono nei `logs/` tracciati in git. Se la routine dimenticasse il flag,
il campo mancherebbe, e "campo assente" vale `null` → nessuna nuova apertura:
fail-closed anche così.

### Telemetria: `logs/regime_runs.jsonl`

Una riga per run: esito, exit code, tempo, verdetto per asset, notifiche, eventi
di registro caricati, e **`prev_run_ts`**, il timestamp della riga precedente
trovata nel file. Se il canale di persistenza funziona, ogni run vede la
precedente: è la prova, run dopo run, che il registro sopravvive al container.
Sincronizzato su `main` con `git_push_log.py --also`.

### STEP 0 fuori portata — per costruzione

Nella routine l'housekeeping (manage_positions, intraday_exit, modifiche SL,
chiusure) gira **prima** dello STEP 2, cioè prima che il regime esista. Il regime
non può bloccarlo nemmeno volendo (§0.1).

---

## 12. Dove vive la configurazione della routine

**La routine non sta nel repo.** È il trigger schedulato
`trig_01HJ3fU1mnX1qJj3ZmfkweG8` ("Trading Bot — BTC/ETH/SOL Hourly"), e il suo
prompt contiene il flusso completo della run — incluse le chiamate a
`market_summary.py` (STEP 2) e a **`git_push_log.py` (STEP 8)**.

- **Cron:** `0 7-23/2 * * *` → una run **ogni 2 ore, dalle 07 alle 23 UTC** (9 al
  giorno, nessuna di notte). Il nome del trigger ("Hourly") e `CLAUDE.md` ("ogni
  60 min") non corrispondono al cron: segnalato, non corretto (non richiesto).
- **`persist_session: false`:** ogni scatto crea una sessione nuova.
- Il prompt **ripete** il flusso di `CLAUDE.md` in dettaglio: ogni modifica al
  flusso va fatta **in entrambi i posti**, altrimenti le due istruzioni si
  contraddicono. Il prompt si modifica solo mostrando prima il diff esatto e con
  l'ok esplicito del proprietario.

---

## 13. Run sovrapposte — cosa risulta dai log (30/09/2026)

L'ordinamento per timestamp del registro (§9) era una **precauzione teorica**,
non una risposta a un caso osservato. Verifica sui log di `main`:

- `logs/proposals.jsonl`, 601 righe (25/06 → 30/09): **0 righe fuori ordine**. I 52
  gruppi di righe ravvicinate (< 5 min) sono tutti "proposal + un solo esito
  finale", cioè **una sola run** che scrive più righe; nessun gruppo con due esiti.
- 294 commit `bot: run` (27/08 → 30/09), uno per fine run: **una sola coppia**
  entro 60 minuti — 27/08 13:12 e 13:23, il primo giorno del meccanismo di push.
  Senza orari di inizio non si può dire se si siano sovrapposte o susseguite.
  Nessuna fine run fuori dagli orari del cron. Prima del 27/08 compaiono righe
  alle 10 e alle 14 UTC (inizio luglio): run lanciate a mano in sviluppo.
- **Strutturalmente è possibile ma improbabile:** l'attesa più lunga di una run
  è la conferma Telegram (max 30 min), lontana dalle 2 ore fra due run. Il
  vettore realistico è **una run lanciata a mano durante una schedulata**.
- **Nessuna protezione oggi fra container diversi:** `telegram_lock.py` è un lock
  su file in `data/`, locale al container; due run in cloud non lo condividono.
  Se si sovrapponessero potrebbero proporre due trade nella stessa finestra
  (MAX 1 è per run) e contendersi `getUpdates` su Telegram (409). Non risolto:
  segnalato.
- **Durata reale di una run:** la run delle 13:10 del 30/09 è terminata alle
  13:16:44 (~6 minuti), molto lontano dalle 2 ore fra due run.
- **Mitigazione adottata (30/09/2026):** il proprietario **non lancia run
  manuali**. Elimina il vettore realistico di sovrapposizione finché non esiste
  un lock condiviso fra container.

---

## Domande aperte

- **Il ~30% di ore escluse lontane dalle transizioni** (§10): cosa sono? Da
  analizzare (richiesta del 30/09, non bloccante).

---

## 14. Attivazione e piano di ritorno

### Cosa cambia con l'attivazione

1. Merge su `main` del branch `claude/regime-detection-ml-g0362h` (codice,
   modelli, `CLAUDE.md`).
2. Prompt della routine `trig_01HJ3fU1mnX1qJj3ZmfkweG8` sostituito con la
   versione nuova.

Si fanno **nello stesso intervallo fra due run**, subito dopo una run terminata:
metà attivazione blocca il trading (prompt nuovo + codice vecchio → flag
sconosciuto, la routine si ferma; codice nuovo + prompt vecchio → campo assente,
nessuna apertura).

### Copie dei prompt nel repo

| File | Contenuto | SHA-256 |
|---|---|---|
| `ops/routine_prompt_pre_regime.txt` | prompt **prima** dell'attivazione, testo integrale | `a9a773db5c085ca2356014f2fbc2892544c38d9d7f595ef7d9d00d3e431fe6d2` |
| `ops/routine_prompt_regime.txt` | prompt **dopo** l'attivazione | `33fb8965623f20e05f2da2f56bbca52d1e5f9f7957886d74970940b4df4b8682` |

Il testo "prima" è stato trascritto due volte, indipendentemente, dalla lettura
del trigger: le due copie sono identiche al byte. I file non hanno intestazioni:
sono il prompt esatto, da incollare così com'è. Fonte di verità resta il
trigger; queste sono copie di revisione e di ripristino.

### Piano di ritorno — pochi minuti, in quest'ordine

Si esegue **nello stesso intervallo fra due run**, subito dopo una run terminata
(stato del trigger: `last_run.finished_at`).

**1. Prompt (≈ 1 minuto).** Ripristinare il testo di
`ops/routine_prompt_pre_regime.txt`:
- da una sessione Claude: `update_trigger(trigger_id="trig_01HJ3fU1mnX1qJj3ZmfkweG8",
  prompt=<contenuto esatto del file>)`, poi `get_trigger` e confronto col file;
- a mano: interfaccia delle Routine su claude.ai → incollare il file.

**2. Codice (≈ 3–5 minuti).** Annullare il merge su `main` con un commit di
revert (la storia non si riscrive):
- a mano, la via più rapida: pagina della PR su GitHub → **Revert** → merge
  della PR di revert;
- da una sessione Claude (il proxy blocca il push diretto su `main`):

      git fetch origin main
      MERGE=$(git log origin/main --merges --format=%H -1 \
              --grep 'claude/regime-detection-ml-g0362h')
      git checkout -B claude/revert-regime origin/main
      git revert -m 1 "$MERGE" --no-edit
      git push -u origin claude/revert-regime
      # poi PR verso main e merge

**3. Verifica.** Su `origin/main`, `CLAUDE.md` non contiene la sezione "Regime di
mercato" e `python3 -m unittest discover -s . -p 'test_*.py'` passa; il trigger
restituisce il prompt con SHA-256 `a9a773db…`.

**Perché prima il prompt e poi il codice.** È l'ordine inverso dell'attivazione.
Con il prompt vecchio e il codice ancora nuovo, `market_summary.py` senza flag
produce l'output di prima al byte (§11): il passo 1 da solo non rompe nulla, e
il passo 2 si fa con calma entro lo stesso intervallo. L'ordine opposto (codice
vecchio + prompt nuovo) farebbe fallire `market_summary.py --with-regime` e
fermerebbe la routine.

**Cosa il ritorno NON tocca:** i log `logs/regime_*.jsonl` già su `main` restano
come storico (innocui senza il detector).

### Quando tornare indietro

Delega del proprietario (30/09/2026): Claude decide e applica senza attendere,
poi avvisa.

- **Si torna indietro se:** la routine fallisce o si ferma per l'integrazione
  (errore di `market_summary.py --with-regime`, STEP 8 non eseguito); lo STEP 1
  (protezione) è toccato in qualunque modo; il detector è **strutturalmente**
  inutilizzabile nel container della routine (`integrity_failed`, oppure
  `source_unavailable` su tutti gli asset, cioè Coinbase irraggiungibile); le
  notifiche si ripetono a ogni run.
- **Non si torna indietro se:** un asset è `null` per una ragione legittima
  (sotto soglia, storico insufficiente); la telemetria non persiste (il
  fail-closed resta intatto) — si segnala e si corregge in avanti.
