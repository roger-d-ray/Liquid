# Trading Bot — istruzioni permanenti

## Obiettivo

Bot quantitativo crypto che analizza BTC, ETH e SOL ogni 60min.
Identifica opportunità con le 3 skill in skills/.
Notifica via Telegram. Aspetta conferma manuale prima di eseguire.

## Modalita account Liquid

Questa routine opera in **live trading**. La modalita deve essere gia dichiarata
nel `.env` o nel runtime prima dell'avvio:

    TRADING_MODE=live
    LIVE_TRADING_ALLOWED=true

Default sicuro del codice: se le variabili mancano, considera `TRADING_MODE=paper`
e blocca le azioni live.

Prima di qualunque azione MCP che modifica il conto (`execute_order`,
   `close_positions_batch`, modifica TP/SL o leva), esegui:
`python trading_mode.py --require-live`. Se fallisce, NON chiamare il tool MCP
e notifica Telegram con il motivo.

Il gate locale NON basta a garantire che Co-Invest sia in live. All'inizio di
ogni run verifica anche la modalita reale del connector Co-Invest con
`paper_trading_status()` o tool equivalente. Se Co-Invest risulta in paper, NON
eseguire `execute_order`, `close_positions_batch` o modifiche posizione: notifica
Telegram e fermati. Non attivare/disattivare paper automaticamente dalla routine.

Il reset paper e' disabilitato in questa routine: non chiamare mai
`enable_paper_trading()` o `reset_paper_account()` e ignora eventuali flag
`data/reset_request.json`.

## Flusso ad ogni run

0. **Housekeeping live + Flatten intraday (PRIMA di tutto):**
   - **0a. Pre-flight live reale:** esegui `python trading_mode.py --require-live`, poi verifica Co-Invest con `paper_trading_status()` o tool equivalente. Se Co-Invest e' in paper, notifica Telegram e fermati. Non chiamare `enable_paper_trading()` o `reset_paper_account()`. Ignora eventuali reset pending e prosegui solo se Co-Invest e' live.
   - **0b. Gestione posizioni aperte:** aggiorna lo snapshot e applica `manage_positions.py` — vedi sezione "Gestione intraday intelligente". Protegge profitti, chiude trade fermi e sostituisce il vecchio max-hold cieco.
   - **0c. Flatten intraday:** esegui `intraday_exit.py` come backstop finale anti-overnight — vedi sezione "Flatten automatico". Va eseguito ad ogni run, prima di cercare nuovi setup, così libera slot/esposizione e garantisce zero overnight.
1. Esegui data_fetcher.py → genera data/market_data.json
   - `timeframes[tf]` contiene esclusivamente `ClosedBar`; `intrabar[tf]` contiene
     snapshot parziali separati. Indicatori, swing e segnali bar-close devono
     usare solo barre con `is_final=true` e `available_at <= timestamp` della run.
2. Leggi market_data.json e applica le 3 skill
3. Per il segnale migliore (confidence più alta), esegui risk_manager.py
4. Se approvato, invia notifica Telegram con telegram_notify.py
5. Se risposta è "accetta", esegui `python trading_mode.py --require-live` e ri-verifica che Co-Invest non sia in paper; solo se entrambi passano chiama execute_order() di Co-Invest (esecuzione diretta — la conferma Telegram è l'unica autorizzazione richiesta)
   Subito dopo l'esecuzione, chiama get_portfolio() e invia su Telegram un messaggio di conferma con riepilogo portafoglio (equity, margine usato, disponibile, posizioni aperte)
6. Logga il risultato in logs/proposals.jsonl
7. **Aggiorna lo snapshot E fai il push del portafoglio su Telegram** (ad ogni run, anche senza trade):
   - `get_portfolio()` → `portfolio.from_coinvest(gp)` → `portfolio.save_portfolio_state(snap)`.
   - **Allega SEMPRE il riepilogo portafoglio al messaggio Telegram di fine run** (quello di trade eseguito / rifiutato / "nessun setup"). Usa `portfolio.build_portfolio_message()` o una riga compatta (equity, disponibile, margine, N posizioni). Questo è il modello **push** che sostituisce il poller `/portfolio` (vedi sezione dedicata): niente processo persistente, portafoglio sempre fresco in chat.
8. MAX 1 proposta per run (solo la migliore per confidence)

## Dettaglio STEP 5 — Esecuzione e notifica post-trade

Dopo approvazione Telegram (exit code 0 da telegram_notify.py):

1. Esegui `python trading_mode.py --require-live` e ri-verifica che Co-Invest non sia in paper, poi chiama `execute_order()` via Co-Invest MCP con i parametri validati dal risk_manager:
   - symbol: asset (es. "BTC")
   - side: "buy" (long) o "sell" (short)
   - size: notionale in USD. Dimensiona sul RISCHIO, non sulla leva: `rischio_$ = risk_pct × equity` (risk_pct = 3–5%, vedi Modalità intraday), poi `size_coin = rischio_$ / |entry − stop_loss|`, `size = size_coin × entry`. La leva NON entra nel calcolo del rischio: determina solo il margine impegnato (`margine = size / leverage`) e la distanza di liquidazione.
   - leverage: scalabile per confidence, **max 20x** (validato in risk_manager.py → MAX_LEVERAGE). Usa leve alte SOLO con stop stretti su ATR 15m. Default suggerito: 10x setup normali, fino a 20x sui setup a confidence più alta.
   - type: "market"
   - tp: target
   - sl: stop_loss
   - reasoning: stringa con la motivazione tecnica sintetica

   ⚠️ **La size va calcolata PRIMA della notifica, non dopo l'approvazione**, così il messaggio Telegram mostra quanto stai investendo. Usa `sizing.plan_trade(proposal, snapshot)` (snapshot = `portfolio.from_coinvest(get_portfolio())`, con `total_equity`/`available_balance`/`positions`). `plan_trade`:
   - calcola `size_usd` (notionale), `margin_usd`, `risk_usd`;
   - se il margine del nuovo trade **non rientra** nel budget (cap per-asset 40% / totale 60% / disponibile), **alza la leva** fino al tetto utile (≤ MAX_LEVERAGE e ≤ tetto liq-safe) per farlo rientrare;
   - se non rientra **neanche** alla leva massima utile, riduce il nozionale al massimo consentito dal budget senza modificare entry, stop o target;
   - accetta la size ridotta solo se il rischio effettivo conserva almeno il **75% del rischio ideale** e non scende mai sotto lo **0,75% dell'equity**: `actual_risk_pct >= max(target_risk_pct × 0.75, 0.0075)`; altrimenti ritorna `fits=False`.

   Comportamento richiesto:
   - Se `fits=False`: **NON notificare la proposta**. Invia `python telegram_notify.py --message "❌ Trade scartato (margine/rischio minimo): <reason>"`, logga `{"result":"discarded_margin","reason":...}`, prosegui.
   - Se `fits=True`: scrivi nel proposal `size_usd`, `margin_usd`, `risk_usd`, `equity`, `risk_pct`, `leverage` (valori **effettivi** dell'ordine). Conserva il rischio ideale in `target_risk_pct`/`target_risk_usd`. Se la size è stata ridotta, includi `size_adjusted=true` e `risk_retention`; se la leva è stata alzata, includi `requested_leverage` e `leverage_adjusted=true`. Poi chiama `telegram_notify.py`.
   - `send_proposal`/`format_proposal` mostrano "💰 Investito / 🔒 Margine / ⚠️ Rischio se SL" (+ eventuale nota leva). Alla conferma, passa a `execute_order()` lo **stesso** `size_usd` e la **stessa** `leverage` finale (unica fonte di verità: nessuna divergenza tra ciò che approvi e ciò che viene eseguito).
2. Dopo execute_order(), controlla la risposta: se contiene `exchange="paper"` o un indicatore equivalente di paper trading, considera la run NON live, notifica l'errore e fermati senza descriverla come trade reale. Se la risposta conferma live, chiama `get_portfolio()` e costruisci il messaggio Telegram:

   ✅ Trade eseguito! [emoji] [ASSET] [SIGNAL.upper()] · [leverage]x · $[size]
   Entry: $[entry] Take Profit: $[target] Stop Loss: $[stop_loss] R/R: [rr_ratio] Confidence: [conf]%
   📊 Portafoglio aggiornato: Equity: $[equity] Margine usato: $[margin_used] Disponibile: $[available_balance] Posizioni aperte: [N]

3. Invia il messaggio con: python telegram_notify.py --message "<testo>"
4. Persisti lo snapshot per /portfolio: `portfolio.save_portfolio_state(portfolio.from_coinvest(gp))` usando il `gp` appena ottenuto da `get_portfolio()` (riusa la stessa chiamata del punto 2 — non serve interrogare due volte).

## Modalità intraday aggressiva (orizzonte di giornata)

Profilo operativo corrente: **scalping/intraday aggressivo**. Le posizioni nascono per aprirsi e chiudersi in giornata, mai overnight. Le skill in skills/ restano la fonte di verità — sono agnostiche all'orizzonte (momentum-trading e range-trading coprono esplicitamente l'intraday); cambia SOLO su che dati le applichi e come esci.

- **Timeframe di analisi:** primario **15m e 1h** (già calcolati in market_data.json). NON usare 4h/1d per generare il segnale: servono solo come contesto di direzione macro. Il campo `timeframe` del proposal deve riflettere 15m/1h.
- **Skill da privilegiare:** **momentum-trading** e **range-trading** (le due intraday). **trend-following** è usata solo come filtro di direzione (EMA50/200 a 1h): non come generatore di segnali intraday, perché il suo orizzonte è settimane/mesi.
- **Leva:** scalabile per confidence, **massimo 20x** (tetto forzato in risk_manager.py). Alte leve solo con stop stretti.
- **Rischio per trade:** **3–5% dell'equity** (`risk_pct`). Vedi formula size nello STEP 5.
- **Uscita = TP/SL stretti intraday + gestione progressiva + flatten finale.** Dimensiona TP e SL sull'**ATR a 15m** così la posizione si risolve in fretta (uscita primaria). Poi `manage_positions.py` protegge o chiude in base al progresso verso il TP. Infine `intraday_exit.py` resta il **flatten 100% automatico** che garantisce zero overnight (vedi sezioni dedicate). ⚠️ Conseguenza a 20x: un gap o un wick oltre lo stop può liquidare/eseguire a prezzo peggiore — lo stop stretto e ancorato a struttura resta la prima protezione. Se un setup non consente uno stop stretto e coerente, **è un no-trade**.
- **R/R minimo** 1.2 (hard) come da risk_manager; sotto 1.8 è comunque un warning.

## Gestione intraday intelligente (prima del flatten)

Meccanismo che evita il vecchio max-hold cieco: prima di chiudere per tempo, guarda se il trade ha camminato verso il TP.

- **Decisione (Python, no credenziali):** `manage_positions.py` legge `data/portfolio_state.json` e stampa un array JSON di azioni:
  - `modify_sl`: protezione del profitto via modifica SL, preservando il TP esistente.
  - `close`: chiusura via `close_positions_batch` quando conviene incassare o liberare margine.
- **Guardrail principali:**
  1. **Mai peggiorare lo stop:** long = SL solo più alto; short = SL solo più basso.
  2. **50% verso TP:** sposta SL a breakeven con buffer (`BE_AT_PROGRESS=0.5`, `BE_BUFFER_PCT=0.001`).
  3. **75% verso TP:** trailing stop, bloccando metà del percorso fatto (`TRAIL_AT_PROGRESS=0.75`, `LOCK_FRACTION=0.5`).
  4. **90% verso TP:** chiude la posizione se il TP non è stato fillato (`PROFIT_PROTECT_CLOSE=1`, `CLOSE_AT_PROGRESS=0.9`).
  5. **Trade fermo:** dopo 3h chiude solo se il progresso è sotto 25% (`STALE_AFTER_HOURS=3`, `STALE_MIN_PROGRESS=0.25`).
  6. **Max-hold intelligente:** dopo 6h chiude solo se il progresso è sotto 50% (`MAX_HOLD_HOURS=6`, `MAX_HOLD_MIN_PROGRESS=0.5`).
  7. **Leva:** non aumentare mai la leva di una posizione aperta in automatico; al massimo ridurla dietro una regola esplicita futura.
- **Esecuzione (agente, via MCP):**
  1. `gp = get_portfolio()` → `portfolio.save_portfolio_state(portfolio.from_coinvest(gp))`.
  2. `python manage_positions.py` → leggi l'array JSON.
  3. Per ogni `modify_sl`, esegui `python trading_mode.py --require-live`, poi chiama il tool MCP di modifica posizione aggiornando solo lo stop loss e preservando take profit, size e lato riportati dall'azione.
  4. Raggruppa tutte le azioni `close`; prima di chiudere esegui `python trading_mode.py --require-live`, poi chiudi con una sola `close_positions_batch(confirmed=true, symbols=[...])`.
  5. Se hai modificato o chiuso qualcosa: ri-esegui `get_portfolio()`, salva lo snapshot e notifica Telegram con elenco azioni e motivi.
  6. Poi esegui comunque il flatten finale (`intraday_exit.py`), perché la garanzia zero overnight resta separata.

## Flatten automatico (garanzia "chiude in giornata")

Meccanismo che rende l'uscita intraday **100% automatica, senza intervento umano**.

- **Chi lo triggera:** la routine oraria stessa. Il cron che fa girare la routine ogni 60 min *è* il trigger — nessun demone separato, nessun umano. Ad ogni run l'agente esegue lo STEP 0.
- **Decisione (Python, no credenziali):** `intraday_exit.py` legge lo snapshot `data/portfolio_state.json` e stampa su stdout un array JSON delle posizioni da chiudere. La regola normale è una sola:
  1. **Flatten di fine giornata (garanzia dura):** oltre `FLATTEN_HOUR_UTC` (default 23) chiude TUTTE le posizioni aperte → mai overnight. Non richiede l'orario di apertura, quindi funziona sempre.
  Il vecchio max-hold cieco è disattivato di default; se serve come emergenza legacy usa `FLATTEN_MAX_HOLD_HOURS>0`. Il max-hold normale ora è progress-aware in `manage_positions.py`.
- **Esecuzione (agente, via MCP):** la chiusura vera è un'azione MCP. ⚠️ L'UNICO tool di chiusura chiamabile dall'agente è **`close_positions_batch`**. Il tool singolare `close_position` è SYSTEM INTERNAL e **non va MAI chiamato** dall'agente. `close_positions_batch` è pre-autorizzato dalla policy intraday (stessa logica di `execute_order`: la policy sostituisce il widget di conferma). Lo STEP 0 è:
  1. `gp = get_portfolio()` → `portfolio.save_portfolio_state(portfolio.from_coinvest(gp))` (aggiorna lo snapshot con lo stato reale).
  2. `python intraday_exit.py` → leggi l'array JSON su stdout: `[{"symbol":"BTC-PERP","asset":"BTC","side":"long","reason":"..."}]`.
  3. Se l'array NON è vuoto, esegui `python trading_mode.py --require-live`, poi chiudi via Co-Invest MCP con **una sola** chiamata:
     `close_positions_batch(confirmed=true, symbols=[<lista dei "symbol" perp dell'array>])`.
     (I `symbol` sono in formato perp, es. "BTC-PERP" — passa quelli, non l'`asset`. Con `symbols` omesso chiuderebbe TUTTE le posizioni: passa sempre la lista esplicita.)
  4. Se hai chiuso qualcosa: ri-esegui `get_portfolio()`, ri-salva lo snapshot, e notifica su Telegram (`python telegram_notify.py --message "..."`) con l'elenco di cosa è stato flattato e il motivo.
  5. Se l'array è vuoto: nessuna chiusura, prosegui.
- Lo STEP 0 non apre mai posizioni: chiude soltanto. È indipendente dalla proposta di trading (STEP 1-6).

## Reset paper trading legacy

Questa sezione e' solo documentazione legacy per manutenzione manuale fuori dalla
routine live. La routine live NON deve usarla.

In `TRADING_MODE=live`:
`paper_reset.pending()` restituisce `None`, `paper_reset.request_reset()` rifiuta
la richiesta, e l'agente non deve mai chiamare `reset_paper_account()`.

Se vuoi davvero azzerare un paper account, fallo solo in una sessione interattiva
separata e solo dopo avere impostato esplicitamente `TRADING_MODE=paper`.

Non includere istruzioni di reset paper nel prompt routine live.

## Regole assolute

- Il flatten intraday (STEP 0) va eseguito ad ogni run, anche senza nuovi trade: è la garanzia che nulla resta overnight
- Senza market_data.json aggiornato: fermati e notifica su Telegram
- Se nessuna skill supera confidence 0.55: manda "Nessun setup valido"
- Leva mai oltre 20x: risk_manager.py rifiuta il proposal (MAX_LEVERAGE)
- Analisi e segnale su 15m/1h, mai su 4h/1d (4h/1d = solo contesto)
- Nessuna credenziale nel codice: leggi sempre da variabili d'ambiente
- In live, prima di qualunque ordine/chiusura/modifica posizione deve passare `python trading_mode.py --require-live`
- In live deve passare anche il controllo Co-Invest: se `paper_trading_status()` o la risposta MCP indicano paper, fermati e non eseguire azioni conto
- In live, il reset paper e' vietato: non chiamare `reset_paper_account()`
- Le skill in skills/ sono la fonte di verità: non ignorarle mai

## Portafoglio su Telegram — modello PUSH (attivo) vs poller /portfolio (dormiente)

⚠️ **Architettura attuale: routine nel CLOUD, nessun host always-on.** Il poller persistente (`telegram_bot.py`) e il ponte a file richiedono che lettore e scrittore stiano sulla **stessa macchina** — condizione non soddisfatta (routine cloud, `data/` git-ignored non attraversa git). Quindi:

- **ATTIVO — push del portafoglio (STEP 7):** ad ogni run la routine allega il riepilogo del portafoglio al messaggio Telegram che già invia. Nessun processo persistente, nessun 409, portafoglio fresco ogni ~60 min. Questo è il meccanismo in uso.
- **DORMIENTE — poller `/portfolio` (`telegram_bot.py`):** funziona solo con un host always-on che condivide il filesystem con la routine. Tenuto per uso futuro; NON attivo con il setup cloud attuale. La sezione qui sotto lo descrive per quel caso.
- **Reset paper:** non disponibile nella routine live. Usare solo manutenzione manuale separata in `TRADING_MODE=paper`.

### (Dormiente) Comando Telegram /portfolio (sola lettura)

Comando on-demand per consultare il portafoglio, **separato dal flusso di trading (STEP 0–6)**: non esegue, modifica o chiude mai ordini. Richiede `telegram_bot.py` in esecuzione su un host always-on (vedi nota sopra).

- Listener: `telegram_bot.py` — processo **persistente** che fa long-poll di `getUpdates` e risponde ai comandi. Comandi: `/portfolio`, `/help`, `/start`.
- Avvio: `python telegram_bot.py` (Ctrl-C per fermare). `--once` esegue un solo ciclo di poll (test).
- Fonte dati: `data/portfolio_state.json` (Opzione B). Lo snapshot è popolato dall'**assistente Co-Invest MCP** che chiama `get_portfolio()` durante il routine 60-min e scrive il file. `/portfolio` legge **solo** la cache e la formatta (`portfolio.py`) — nessuna credenziale exchange richiesta.
- Formattazione: `portfolio.py` → `build_portfolio_message()`. Mostra equity, disponibile, margine usato, e per ogni posizione asset/side/leva/size, entry, mark, PnL. Reader tollerante ai sinonimi di chiave (es. `total_equity`/`equity`, `signal`/`side`, `notional`/`size_usd`).
- Errori: se lo snapshot manca o è illeggibile, il bot **invia su Telegram il dettaglio dell'errore** invece di crashare.
- ⚠️ Vincolo single-consumer: Telegram ammette **un solo** consumatore `getUpdates` per bot. Non far girare `telegram_bot.py` in contemporanea a `wait_response()` di `telegram_notify.py` sullo stesso `TELEGRAM_BOT_TOKEN` (→ HTTP 409). Usare bot separati o mettere in pausa il poller mentre una proposta è in attesa di approvazione.
- Upgrade futuro (Opzione A, non implementato): interrogazione diretta dell'API dell'exchange con chiavi in `.env`. Richiede credenziali dedicate (es. `HYPERLIQUID_API_KEY` + secret / wallet) — da aggiungere solo dietro esplicita configurazione, mai in chiaro nel codice.

### Per l'agente MCP: come popolare lo snapshot

L'account Liquid è collegato **via Co-Invest MCP** (autenticato all'agente): `get_portfolio()` ritorna i dati dell'account nella modalità attiva (paper o live). Gli script Python NON hanno chiavi API dell'exchange — l'unico ponte all'account è l'MCP, quindi **è l'agente** che deve popolare lo snapshot.

Il mapping dei campi MCP (`entryPx`/`markPx`, `size` in unità coin, `displayName`) verso lo schema dello snapshot è già in `portfolio.py`. Non scrivere il JSON a mano: usa gli helper.

    import portfolio
    gp   = get_portfolio()                         # payload Co-Invest MCP
    snap = portfolio.from_coinvest(gp)             # mappa MCP → schema snapshot
    portfolio.save_portfolio_state(snap)          # scrive data/portfolio_state.json

`from_coinvest` calcola anche `size_usd = |size| × markPx` e usa `displayName` come asset. Nota: `data/` è in `.gitignore`, quindi lo snapshot resta locale.

Opzione A (client REST diretto con chiavi in `.env`) resta **non necessaria** finché l'MCP è il ponte: servirebbe solo per dati live al secondo anche quando l'agente non gira.

## Stack

- Python 3.11+, requests, pandas, numpy, python-telegram-bot
- Dati storici (OHLCV): **Kraken public API** (gratuita, no auth) = fonte PRIMARIA — non geo-blocca gli IP cloud, 4h nativo. **Coinbase Exchange API** = fallback automatico (4h aggregato da 1h). Il passaggio Kraken→Coinbase è trasparente in data_fetcher.py.
- Prezzo spot / 24h (price, change, volume): Kraken Ticker (primario) → Binance spot (fallback)
- Funding / OI / long-short ratio: Binance Futures API (gratuita, no auth) — degrada a None se irraggiungibile (es. IP cloud bloccati con HTTP 451), poi arricchito via Co-Invest MCP
- Dati live aggiuntivi (positioning, news, unusual): Co-Invest MCP
- Trading: Co-Invest execute_order() dopo approvazione Telegram (la conferma Telegram sostituisce il widget di conferma Claude)

## Formato JSON proposta (standard tra skill e risk_manager)

{
"strategy": "range_trading|trend_following|momentum_trading",
"asset": "BTC|ETH|SOL",
"side": "long|short",
"entry": float,
"tp": float, "sl": float,
"leverage": float,
"risk_pct": float, "rr_ratio": float,
"confidence": float (0.0-1.0),

// Sizing — calcolato da sizing.compute_size() PRIMA della notifica (mostrato in proposta):
"equity": float,        // equity del conto (da get_portfolio)
"target_risk_pct": float, // rischio ideale richiesto dalla strategia
"target_risk_usd": float, // target_risk_pct × equity
"risk_pct": float,      // rischio effettivo dell'ordine (può essere ridotto)
"risk_usd": float,      // risk_pct × equity = € persi se scatta lo SL
"risk_retention": float, // risk_pct / target_risk_pct
"size_adjusted": bool,  // true se il nozionale è stato ridotto per il margine
"size_usd": float,      // notionale investito (= size_coin × entry)
"margin_usd": float     // size_usd / leverage = capitale realmente impegnato
}
