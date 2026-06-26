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
5. Se risposta è "accetta", chiama suggest_trade() di Co-Invest
6. Logga il risultato in logs/proposals.jsonl
7. MAX 1 proposta per run (solo la migliore per confidence)

## Regole assolute

- Senza market_data.json aggiornato: fermati e notifica su Telegram
- Se nessuna skill supera confidence 0.55: manda "Nessun setup valido"
- Nessuna credenziale nel codice: leggi sempre da variabili d'ambiente
- Le skill in skills/ sono la fonte di verità: non ignorarle mai

## Stack

- Python 3.11+, requests, pandas, numpy, python-telegram-bot
- Dati storici (OHLCV): Coinbase Exchange API (gratuita, no auth) — 4h aggregato da 1h
- Funding / OI / long-short ratio: Binance Futures API (gratuita, no auth)
- Dati live aggiuntivi (positioning, news, unusual): Co-Invest MCP
- Trading: Co-Invest suggest_trade() dopo approvazione utente

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
