# Trading Bot — istruzioni permanenti

## Obiettivo

Bot quantitativo crypto che analizza BTC, ETH e SOL ogni 60min.
Identifica opportunità con le 3 skill in skills/.
Notifica via Telegram. Aspetta conferma manuale prima di eseguire.

## Flusso ad ogni run

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
   - size: notionale in USD (calcola: ~1% equity a rischio / stop_distance × entry_price × leverage)
   - leverage: 2 (default conservativo)
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

## Regole assolute

- Senza market_data.json aggiornato: fermati e notifica su Telegram
- Se nessuna skill supera confidence 0.55: manda "Nessun setup valido"
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
