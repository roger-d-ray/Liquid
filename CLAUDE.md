# Trading Bot — istruzioni permanenti

## Obiettivo

Bot quantitativo crypto che analizza BTC, ETH e SOL ogni 60min.
Identifica opportunità con le 3 skill in skills/.
Notifica via Telegram. Aspetta conferma manuale prima di eseguire.

## Flusso ad ogni run

0. **Reset paper + Flatten intraday (PRIMA di tutto):**
   - **0a. Reset paper (se richiesto):** controlla `paper_reset.pending()`. Se c'è una richiesta, esegui il reset via MCP — vedi sezione "Reset paper trading". Dopo il reset il conto è vuoto: salta il flatten e prosegui con un conto fresco.
   - **0b. Flatten intraday:** aggiorna lo snapshot e chiudi le posizioni scadute — vedi sezione "Flatten automatico". Va eseguito ad ogni run, prima di cercare nuovi setup, così libera slot/esposizione e garantisce zero overnight.
1. Esegui data_fetcher.py → genera data/market_data.json
2. Leggi market_data.json e applica le 3 skill
3. Per il segnale migliore (confidence più alta), esegui risk_manager.py
4. Se approvato, invia notifica Telegram con telegram_notify.py
5. Se risposta è "accetta", chiama execute_order() di Co-Invest (esecuzione diretta — la conferma Telegram è l'unica autorizzazione richiesta)
   Subito dopo l'esecuzione, chiama get_portfolio() e invia su Telegram un messaggio di conferma con riepilogo portafoglio (equity, margine usato, disponibile, posizioni aperte)
6. Logga il risultato in logs/proposals.jsonl
7. **Aggiorna sempre lo snapshot del portafoglio** (per il comando /portfolio), anche nei run senza trade: `get_portfolio()` → `portfolio.from_coinvest(gp)` → `portfolio.save_portfolio_state(snap)`. Così /portfolio resta fresco ad ogni ciclo (vedi sezione dedicata).
8. MAX 1 proposta per run (solo la migliore per confidence)

## Dettaglio STEP 5 — Esecuzione e notifica post-trade

Dopo approvazione Telegram (exit code 0 da telegram_notify.py):

1. Chiama `execute_order()` via Co-Invest MCP con i parametri validati dal risk_manager:
   - symbol: asset (es. "BTC")
   - side: "buy" (long) o "sell" (short)
   - size: notionale in USD. Dimensiona sul RISCHIO, non sulla leva: `rischio_$ = risk_pct × equity` (risk_pct = 3–5%, vedi Modalità intraday), poi `size_coin = rischio_$ / |entry − stop_loss|`, `size = size_coin × entry`. La leva NON entra nel calcolo del rischio: determina solo il margine impegnato (`margine = size / leverage`) e la distanza di liquidazione.
   - leverage: scalabile per confidence, **max 20x** (validato in risk_manager.py → MAX_LEVERAGE). Usa leve alte SOLO con stop stretti su ATR 15m. Default suggerito: 10x setup normali, fino a 20x sui setup a confidence più alta.
   - type: "market"
   - tp: target
   - sl: stop_loss
   - reasoning: stringa con la motivazione tecnica sintetica
2. Dopo execute_order(), chiama `get_portfolio()` e costruisci il messaggio Telegram:

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
- **Uscita = TP/SL stretti intraday + flatten automatico (backstop).** Dimensiona TP e SL sull'**ATR a 15m** così la posizione si risolve in fretta (uscita primaria). In più, `intraday_exit.py` fornisce un **flatten 100% automatico** che garantisce zero overnight (vedi sezione dedicata). ⚠️ Conseguenza a 20x: un gap o un wick oltre lo stop può liquidare/eseguire a prezzo peggiore — lo stop stretto e ancorato a struttura resta la prima protezione. Se un setup non consente uno stop stretto e coerente, **è un no-trade**.
- **R/R minimo** 1.2 (hard) come da risk_manager; sotto 1.8 è comunque un warning.

## Flatten automatico (garanzia "chiude in giornata")

Meccanismo che rende l'uscita intraday **100% automatica, senza intervento umano**.

- **Chi lo triggera:** la routine oraria stessa. Il cron che fa girare la routine ogni 60 min *è* il trigger — nessun demone separato, nessun umano. Ad ogni run l'agente esegue lo STEP 0.
- **Decisione (Python, no credenziali):** `intraday_exit.py` legge lo snapshot `data/portfolio_state.json` e stampa su stdout un array JSON delle posizioni da chiudere. Due regole indipendenti (basta una):
  1. **Flatten di fine giornata (garanzia dura):** oltre `FLATTEN_HOUR_UTC` (default 23) chiude TUTTE le posizioni aperte → mai overnight. Non richiede l'orario di apertura, quindi funziona sempre.
  2. **Max-hold (best effort):** se lo snapshot ha `opened_at`, chiude la posizione dopo `MAX_HOLD_HOURS` (default 6h). Inattiva in silenzio se l'orario di apertura non è disponibile dall'MCP.
  Entrambe le soglie sono override via env: `FLATTEN_HOUR_UTC`, `MAX_HOLD_HOURS`.
- **Esecuzione (agente, via MCP):** la chiusura vera è un'azione MCP. ⚠️ L'UNICO tool di chiusura chiamabile dall'agente è **`close_positions_batch`**. Il tool singolare `close_position` è SYSTEM INTERNAL e **non va MAI chiamato** dall'agente. `close_positions_batch` è pre-autorizzato dalla policy intraday (stessa logica di `execute_order`: la policy sostituisce il widget di conferma). Lo STEP 0 è:
  1. `gp = get_portfolio()` → `portfolio.save_portfolio_state(portfolio.from_coinvest(gp))` (aggiorna lo snapshot con lo stato reale).
  2. `python intraday_exit.py` → leggi l'array JSON su stdout: `[{"symbol":"BTC-PERP","asset":"BTC","side":"long","reason":"..."}]`.
  3. Se l'array NON è vuoto, chiudi via Co-Invest MCP con **una sola** chiamata:
     `close_positions_batch(confirmed=true, symbols=[<lista dei "symbol" perp dell'array>])`.
     (I `symbol` sono in formato perp, es. "BTC-PERP" — passa quelli, non l'`asset`. Con `symbols` omesso chiuderebbe TUTTE le posizioni: passa sempre la lista esplicita.)
  4. Se hai chiuso qualcosa: ri-esegui `get_portfolio()`, ri-salva lo snapshot, e notifica su Telegram (`python telegram_notify.py --message "..."`) con l'elenco di cosa è stato flattato e il motivo.
  5. Se l'array è vuoto: nessuna chiusura, prosegui.
- Lo STEP 0 non apre mai posizioni: chiude soltanto. È indipendente dalla proposta di trading (STEP 1-6).

## Reset paper trading (comando Telegram)

Comando on-demand per azzerare il paper account e ripartire da 10.000$. La conferma avviene **sempre su Telegram**.

- **Perché due pezzi:** `reset_paper_account()` è un'azione **MCP**, quindi solo l'agente (routine) può eseguirla — `telegram_bot.py` non ha credenziali (come `/portfolio`). Il bot gestisce comando + conferma e **registra la richiesta** in un flag file; l'agente la esegue al ciclo successivo.
- **Lato bot (`telegram_bot.py`):**
  1. L'utente invia `reset paper trading` (o `/reset_paper`). Il bot risponde chiedendo conferma.
  2. L'utente invia `CONFERMA RESET` (o `/conferma_reset`) entro 2 minuti → il bot chiama `paper_reset.request_reset()` che scrive `data/reset_request.json`.
  3. Fuori finestra o senza richiesta attiva → conferma rifiutata, nessun flag.
- **Lato agente (STEP 0a, ad ogni run):**
  1. `import paper_reset` → `req = paper_reset.pending()`. Se `None`, salta (nessun reset in coda).
  2. Se c'è una richiesta: assicurati di essere in paper (`paper_trading_status()`; se necessario `enable_paper_trading()`), poi chiama **`reset_paper_account()`** via Co-Invest MCP.
  3. Ricostruisci lo snapshot (`get_portfolio()` → `portfolio.save_portfolio_state(portfolio.from_coinvest(gp))`) così `/portfolio` mostra i 10.000$ freschi.
  4. Notifica su Telegram (`python telegram_notify.py --message "♻️ Paper account resettato: riparto da $10.000, nessuna posizione aperta."`).
  5. `paper_reset.clear()` per rimuovere il flag (altrimenti il reset si ripeterebbe ogni run).
  6. Dopo il reset il conto è vuoto → salta il flatten (0b) e prosegui la routine normalmente.
- Il flag vive in `data/` (git-ignored): resta locale, mai committato.

## Regole assolute

- Il flatten intraday (STEP 0) va eseguito ad ogni run, anche senza nuovi trade: è la garanzia che nulla resta overnight
- Senza market_data.json aggiornato: fermati e notifica su Telegram
- Se nessuna skill supera confidence 0.55: manda "Nessun setup valido"
- Leva mai oltre 20x: risk_manager.py rifiuta il proposal (MAX_LEVERAGE)
- Analisi e segnale su 15m/1h, mai su 4h/1d (4h/1d = solo contesto)
- Nessuna credenziale nel codice: leggi sempre da variabili d'ambiente
- Le skill in skills/ sono la fonte di verità: non ignorarle mai

## Comando Telegram /portfolio (sola lettura)

Comando on-demand per consultare il portafoglio, **separato dal flusso di trading (STEP 0–6)**: non esegue, modifica o chiude mai ordini.

- Listener: `telegram_bot.py` — processo **persistente** che fa long-poll di `getUpdates` e risponde ai comandi. Comandi: `/portfolio`, `/help`, `/start`.
- Avvio: `python telegram_bot.py` (Ctrl-C per fermare). `--once` esegue un solo ciclo di poll (test).
- Fonte dati: `data/portfolio_state.json` (Opzione B). Lo snapshot è popolato dall'**assistente Co-Invest MCP** che chiama `get_portfolio()` durante il routine 60-min e scrive il file. `/portfolio` legge **solo** la cache e la formatta (`portfolio.py`) — nessuna credenziale exchange richiesta.
- Formattazione: `portfolio.py` → `build_portfolio_message()`. Mostra equity, disponibile, margine usato, e per ogni posizione asset/side/leva/size, entry, mark, PnL. Reader tollerante ai sinonimi di chiave (es. `total_equity`/`equity`, `signal`/`side`, `notional`/`size_usd`).
- Errori: se lo snapshot manca o è illeggibile, il bot **invia su Telegram il dettaglio dell'errore** invece di crashare.
- ⚠️ Vincolo single-consumer: Telegram ammette **un solo** consumatore `getUpdates` per bot. Non far girare `telegram_bot.py` in contemporanea a `wait_response()` di `telegram_notify.py` sullo stesso `TELEGRAM_BOT_TOKEN` (→ HTTP 409). Usare bot separati o mettere in pausa il poller mentre una proposta è in attesa di approvazione.
- Upgrade futuro (Opzione A, non implementato): interrogazione diretta dell'API dell'exchange con chiavi in `.env`. Richiede credenziali dedicate (es. `HYPERLIQUID_API_KEY` + secret / wallet) — da aggiungere solo dietro esplicita configurazione, mai in chiaro nel codice.

### Per l'agente MCP: come popolare lo snapshot

L'account Liquid è collegato **via Co-Invest MCP** (autenticato all'agente): `get_portfolio()` ritorna i dati reali dell'account (attualmente in **paper trading**). Gli script Python NON hanno chiavi API dell'exchange — l'unico ponte all'account è l'MCP, quindi **è l'agente** che deve popolare lo snapshot.

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
"leverage": float, "size_usd": float,
"risk_pct": float, "rr_ratio": float,
"confidence": float (0.0-1.0)
}
