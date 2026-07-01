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
7. MAX 1 proposta per run (solo la migliore per confidence)

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

## Regole assolute

- Senza market_data.json aggiornato: fermati e notifica su Telegram
- Se nessuna skill supera confidence 0.55: manda "Nessun setup valido"
- Nessuna credenziale nel codice: leggi sempre da variabili d'ambiente
- Le skill in skills/ sono la fonte di verità: non ignorarle mai

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
