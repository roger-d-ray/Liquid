# Trading Bot — istruzioni permanenti

## Obiettivo

Analizza BTC, ETH e SOL usando le 3 skill di trading in skills/.
Per ogni opportunità trovata, valida con risk_manager.py.
Se approvata, manda notifica su Telegram e attendi risposta.

## Regole

- Analizza sempre tutti e 3 gli asset
- Non proporre mai più di 1 trade per run
- Se nessun setup raggiunge confidence >= 0.55, non mandare nulla
- Logga sempre il risultato in logs/proposals.jsonl
