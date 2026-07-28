#!/usr/bin/env python3
"""propose_pipeline.py — resolve → validate → size → notify in UN solo processo.

Perché esiste
=============
Il timestamp del book (`execution_price_observed_at`) viene ri-controllato contro
`EXECUTION_DATA_MAX_AGE_SECONDS` (default 30s) in QUATTRO punti della routine:

  1. ExecutionInstrumentResolver.resolve()          (execution_instrument.py)
  2. RiskManager.validate() → execution_block_reason (risk_manager.py)
  3. sizing.plan_trade()    → assert_executable_proposal (sizing.py)
  4. telegram_notify.notify_and_wait() → assert_executable_proposal

Quando l'agente esegue questi quattro passi come tool-call SEPARATE (pensiero +
round-trip MCP + letture di schema in mezzo), il wall-clock tra `show_orderbook`
e l'invio Telegram supera facilmente i 30s: il contesto va "stale", l'agente
rifà `show_orderbook`, e il ciclo si ripete. Storicamente l'agente si costruiva
uno "script combinato" usa-e-getta a ogni run e poi lo cancellava.

Questo modulo rende quello script PERMANENTE: fa resolve → validate → size →
notify dentro lo stesso processo Python (sotto-secondo), così tutti e quattro i
controlli cadono nella stessa frazione di secondo e passano sempre — SENZA
toccare il valore del gate (resta 30s, invariato). L'unica latenza residua è tra
la ricezione di `show_orderbook` (agente) e il lancio di questo script.

Cosa NON fa (di proposito)
==========================
- NON apre l'ordine. La pipeline si ferma all'invio Telegram + attesa risposta.
  Dopo l'approvazione, l'agente ri-risolve una quotazione fresca e chiama
  `execute_order()` via MCP: tutte le azioni che toccano il conto restano
  nell'agente, come richiesto dal CLAUDE.md (STEP 5). La conferma Telegram resta
  l'unica autorizzazione.
- NON inventa venue/simboli: usa CoinvestRuntimeAdapter sui tre payload MCP.
- NON indebolisce nessun gate: risoluzione, dislocazione, freschezza, floor di
  collaterale e di rischio restano quelli di execution_instrument/risk_manager/
  sizing.

Contratto di input (JSON, assemblato dall'agente in-contesto SUBITO dopo un
`show_orderbook` fresco)
========================================================================
{
  "proposal": { ...proposal completo generato dalle skill: strategy, asset,
                 signal|side, timeframe, entry, target|tp, stop_loss|sl,
                 leverage, risk_pct, confidence, signal_price, motivation,
                 + indicatori richiesti dal validator (adx, rsi, ema*, atr,
                 macd_histogram, volume_ratio, support/resistance, ...) },
  "coinvest": {
    "search_markets": { <payload MCP grezzo> },
    "analyze_market": { <payload MCP grezzo> },
    "show_orderbook": { <payload MCP grezzo> }
  },
  "venues": {
    "signal_venue": "kraken",
    "derivatives_venue": "binance_futures" | null,
    "execution_venue": "liquid"   // opzionale; default: venue dell'adapter
  },
  "received_at": "2026-07-28T13:03:36+00:00",  // opzionale: ricezione locale del book
  "snapshot": { "total_equity":..., "available_balance":..., "positions":[...] }
              // opzionale; se assente legge data/portfolio_state.json
}

Output
======
Scrive un oggetto JSON di risultato su --result-file (autorevole) e su stdout
(ultima riga, prefissata da `RESULT_JSON: `). Chiavi principali:
  stage:  "adapter" | "resolve" | "validate" | "size" | "notify"
  ok:     bool (True solo se la catena è arrivata in fondo con successo)
  ...campi specifici per stage (reasons/rejection_reason/reason/accepted)...
  proposal: il proposal finale arricchito (context + sizing) quando disponibile

Exit code (per l'agente / cron)
  0  = accettato su Telegram (o dry-run completato con esito "fits")
  10 = rifiutato / timeout su Telegram
  15 = quotazione andata stale proprio all'invio (freschezza) — vedi nota
  20 = scartato in sizing (floor margine/rischio) — messaggio discard inviato
  30 = rifiutato dal risk manager (validazione)
  40 = non risolto (resolve/adapter: blocked/unsupported) — NESSUN invio
  2  = errore d'uso / input non valido

Uso
===
  python propose_pipeline.py --input run_input.json --result-file run_result.json
  python propose_pipeline.py --input run_input.json --dry-run   # non invia nulla
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


# Alias canonici condivisi con proposal_schema (side→signal, tp→target, sl→stop_loss).
# Applichiamo solo la copia degli alias: la validazione autorevole resta in
# RiskManager.validate() (single source of truth).
_ALIASES = {
    "side": "signal",
    "tp": "target",
    "take_profit": "target",
    "sl": "stop_loss",
    "stop": "stop_loss",
    "rr": "risk_reward",
    "rr_ratio": "risk_reward",
}


class PipelineInputError(ValueError):
    """Input JSON malformato o incompleto per la pipeline."""


# Sentinella: "usa l'audit path di default del resolver" (logs/execution_resolution.jsonl).
# I test passano audit_path=None per non scrivere nei log reali.
_DEFAULT_AUDIT = object()


def _apply_aliases(proposal: dict) -> dict:
    """Copia gli alias verso le chiavi canoniche se mancanti (non valida)."""
    for old, new in _ALIASES.items():
        if new not in proposal and old in proposal:
            proposal[new] = proposal[old]
    return proposal


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PipelineInputError(f"'{name}' deve essere un oggetto JSON")
    return value


def _load_snapshot(payload: Mapping[str, Any]) -> dict:
    snap = payload.get("snapshot")
    if isinstance(snap, Mapping):
        return dict(snap)
    # Fallback: la cache scritta dall'agente via get_portfolio() → from_coinvest().
    import portfolio
    return portfolio.load_portfolio_state()


def _emit(result: dict, result_file: Optional[Path]) -> None:
    text = json.dumps(result, ensure_ascii=False)
    if result_file is not None:
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(text, encoding="utf-8")
    # Riga finale, prefisso robusto: l'agente/cron non deve confondere echo di
    # wrapper con l'esito reale (bug storico "exit 0 = echo, non telegram").
    # Lo stdout su console Windows può essere cp1252: una stampa che fallisce su
    # un'emoji NON deve alterare l'exit code (il result-file resta autorevole).
    line = "RESULT_JSON: " + text
    try:
        print(line)
    except UnicodeEncodeError:
        enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
        sys.stdout.buffer.write(line.encode(enc, errors="backslashreplace"))
        sys.stdout.buffer.write(b"\n")
        sys.stdout.flush()


def _sizing_fields(plan: Mapping[str, Any]) -> dict:
    """Estrae dai risultati di plan_trade i campi effettivi dell'ordine da
    scrivere nel proposal (mostrati in Telegram e usati per execute_order)."""
    keys = (
        "size_usd", "margin_usd", "risk_usd", "equity", "risk_pct", "leverage",
        "target_risk_pct", "target_risk_usd", "risk_retention",
        "size_adjusted", "requested_leverage", "leverage_adjusted",
        "minimum_risk_pct", "target_size_usd",
    )
    return {k: plan[k] for k in keys if k in plan}


def run_pipeline(
    payload: Mapping[str, Any],
    *,
    dry_run: bool,
    timeout_minutes: int,
    audit_path: Any = _DEFAULT_AUDIT,
) -> tuple[dict, int]:
    """Esegue resolve → validate → size → notify. Ritorna (result_dict, exit_code).

    `audit_path`: lasciato al default il resolver logga su
    logs/execution_resolution.jsonl (comportamento di produzione richiesto dal
    CLAUDE.md). Passa `None` per disabilitare il log (usato dai test).
    """
    # ── Import locali: risolvono il codice reale, single source of truth ──────
    from coinvest_market import CoinvestRuntimeAdapter, CoinvestPayloadError
    from execution_instrument import ExecutionInstrumentResolver, RESOLVED
    from risk_manager import RiskManager

    proposal = payload.get("proposal")
    if not isinstance(proposal, Mapping):
        raise PipelineInputError("'proposal' mancante o non è un oggetto JSON")
    proposal = _apply_aliases(dict(proposal))

    coinvest = _require_mapping(payload.get("coinvest"), "coinvest")
    search = _require_mapping(coinvest.get("search_markets"), "coinvest.search_markets")
    analyze = _require_mapping(coinvest.get("analyze_market"), "coinvest.analyze_market")
    book = _require_mapping(coinvest.get("show_orderbook"), "coinvest.show_orderbook")

    venues = payload.get("venues") or {}
    if not isinstance(venues, Mapping):
        raise PipelineInputError("'venues' deve essere un oggetto JSON")
    signal_venue = venues.get("signal_venue")
    derivatives_venue = venues.get("derivatives_venue")
    execution_venue = venues.get("execution_venue")

    received_at = _parse_dt(payload.get("received_at")) or datetime.now(timezone.utc)

    # ── STEP A — Adapter sui tre payload MCP (identità provata, nessun parsing) ─
    try:
        adapter_kwargs = {"received_at": received_at}
        if execution_venue:
            adapter_kwargs["execution_venue"] = str(execution_venue)
        adapter = CoinvestRuntimeAdapter(search, analyze, book, **adapter_kwargs)
    except CoinvestPayloadError as exc:
        return {
            "stage": "adapter", "ok": False,
            "reason": str(exc),
            "note": "Co-Invest non prova un'unica identità di mercato: no-trade.",
        }, 40

    # ── STEP B — Resolver: costruisce e attacca il MarketContext ──────────────
    if audit_path is _DEFAULT_AUDIT:
        resolver = ExecutionInstrumentResolver(adapter)
    else:
        resolver = ExecutionInstrumentResolver(adapter, audit_path=audit_path)
    resolved = resolver.resolve_proposal(
        proposal,
        signal_venue=signal_venue,
        derivatives_venue=derivatives_venue,
        execution_venue=execution_venue,
    )
    context = resolved.get("market_context") or {}
    status = context.get("resolution_status")
    if status != RESOLVED:
        return {
            "stage": "resolve", "ok": False,
            "resolution_status": status,
            "reasons": context.get("reasons") or ["contesto non risolto"],
            "note": "blocked/unsupported = no-trade; nessun invio Telegram.",
        }, 40

    # ── STEP C — Risk manager (validazione + eventuale aggiustamento stop) ─────
    result_v = RiskManager().validate(resolved)
    if not result_v.approved:
        return {
            "stage": "validate", "ok": False,
            "rejection_reason": result_v.rejection_reason,
            "warnings": result_v.warnings,
        }, 30

    # Recepisci i valori validati (lo stop può essere stato allargato su ATR).
    # L'entry resta il prezzo eseguibile (bid/ask) fissato dal resolver.
    resolved["stop_loss"] = result_v.final_stop
    resolved["sl"] = result_v.final_stop
    resolved["target"] = result_v.final_target
    resolved["tp"] = result_v.final_target
    resolved["risk_reward"] = result_v.risk_reward

    # ── STEP D — Sizing: dimensiona e adatta la leva al budget di margine ─────
    from sizing import plan_trade  # import qui: dipende da risk_manager già caricato
    snapshot = _load_snapshot(payload)
    try:
        plan = plan_trade(resolved, snapshot)
    except Exception as exc:  # sizing solleva ValueError su input impossibili
        return {"stage": "size", "ok": False, "fits": False,
                "reason": f"sizing non calcolabile: {exc}"}, 20

    if not plan.get("fits"):
        reason = plan.get("reason") or "margine/rischio minimo non rispettato"
        discard_notified = False
        if not dry_run:
            try:
                from telegram_notify import send_message
                send_message(f"❌ Trade scartato (margine/rischio minimo): {reason}")
                discard_notified = True
            except Exception as exc:  # non deve far crashare la run
                reason += f" | (invio discard fallito: {exc})"
        return {
            "stage": "size", "ok": False, "fits": False,
            "reason": reason,
            "sizing": {k: plan.get(k) for k in (
                "target_risk_pct", "risk_pct", "risk_retention",
                "minimum_risk_pct", "margin_budget", "leverage_ceiling",
            ) if k in plan},
            "discard_notified": discard_notified,
            "asset": resolved.get("asset"),
            "signal": resolved.get("signal"),
        }, 20

    # Scrivi nel proposal i valori EFFETTIVI dell'ordine (mostrati in Telegram e
    # usati poi da execute_order: unica fonte di verità, nessuna divergenza).
    resolved.update(_sizing_fields(plan))

    # ── STEP E — Notifica Telegram (invia + attende risposta reale) ───────────
    if dry_run:
        from telegram_notify import format_proposal
        return {
            "stage": "size", "ok": True, "fits": True, "dry_run": True,
            "message_preview": format_proposal(resolved),
            "proposal": resolved,
        }, 0

    from execution_instrument import ExecutionResolutionError
    from telegram_notify import notify_and_wait
    try:
        accepted = notify_and_wait(resolved, timeout_minutes=timeout_minutes)
    except ExecutionResolutionError as exc:
        # La freschezza è ceduta proprio all'invio: la catena in-process non è
        # bastata (run eccezionalmente lento). NON è un'approvazione.
        return {
            "stage": "notify", "ok": False, "accepted": False,
            "reason": f"stale/non-eseguibile all'invio: {exc}",
            "proposal": resolved,
        }, 15

    return {
        "stage": "notify", "ok": bool(accepted), "accepted": bool(accepted),
        "proposal": resolved,
        "note": (
            "Se accepted=true: l'agente DEVE ri-risolvere una quotazione fresca "
            "e chiamare execute_order() via MCP (la pipeline non esegue ordini)."
        ),
    }, (0 if accepted else 10)


def main() -> int:
    # Su console Windows lo stdout di default è cp1252 e non stampa le emoji del
    # messaggio Telegram: forziamo utf-8 quando possibile (best-effort).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="resolve → validate → size → notify in un solo processo."
    )
    parser.add_argument("--input", "-i", help="File JSON di input (contratto sopra).")
    parser.add_argument("input_pos", nargs="?", help="Alias posizionale di --input.")
    parser.add_argument("--result-file", "-o", help="Dove scrivere il risultato JSON.")
    parser.add_argument("--dry-run", "--preview", action="store_true",
                        help="Esegue resolve/validate/size ma NON invia nulla.")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Minuti di attesa risposta Telegram (default 30).")
    args = parser.parse_args()

    input_path = args.input or args.input_pos
    if not input_path:
        parser.error("input mancante: --input <file.json>")
    result_file = Path(args.result_file) if args.result_file else None

    try:
        raw = Path(input_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        _emit({"stage": "input", "ok": False, "reason": f"input illeggibile: {exc}"},
              result_file)
        return 2

    try:
        result, code = run_pipeline(
            payload, dry_run=args.dry_run, timeout_minutes=args.timeout,
        )
    except PipelineInputError as exc:
        _emit({"stage": "input", "ok": False, "reason": str(exc)}, result_file)
        return 2
    except Exception as exc:  # non lasciar morire la run senza un risultato scritto
        _emit({"stage": "error", "ok": False, "reason": f"{type(exc).__name__}: {exc}"},
              result_file)
        return 2

    _emit(result, result_file)
    return code


if __name__ == "__main__":
    sys.exit(main())
