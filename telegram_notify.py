"""
telegram_notify.py
Sends trade proposals to Telegram and waits for user approval.

Robust against HTTP 409 (Conflict): Telegram allows only one consumer of a bot's
update stream at a time, so a leftover webhook or a second polling process makes
getUpdates fail with 409. This module recovers autonomously by calling
deleteWebhook(drop_pending_updates) before polling and again on every 409, then
retrying with backoff — no manual intervention needed.

Env vars required:
  TELEGRAM_BOT_TOKEN  — bot token from BotFather
  TELEGRAM_CHAT_ID    — target chat/user ID
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from execution_instrument import assert_executable_proposal

try:
    import telegram_lock as _lock
except Exception:  # coordination is optional; never block trading if it's absent
    _lock = None

WAIT_SECONDS  = 30 * 60   # default timeout
POLL_TIMEOUT  = 30        # seconds per getUpdates long-poll
MAX_RETRIES   = 3
ACCEPT_WORDS  = {"accetta", "si", "sì", "ok", "yes", "y"}


# ─── Credentials ──────────────────────────────────────────────────────────────

def _load_dotenv():
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _creds() -> tuple[str, str]:
    _load_dotenv()
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()
    if not token or not chat_id:
        raise EnvironmentError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as environment variables."
        )
    return token, chat_id


# ─── HTTP with retry ──────────────────────────────────────────────────────────

# HTTP statuses worth retrying instead of failing hard:
#   409 = Conflict  -> another webhook/getUpdates is holding the update stream
#   429 = rate limit, 5xx = transient server errors
_TRANSIENT_STATUS = {409, 429, 500, 502, 503, 504}


def _retry_after(body: str, attempt: int) -> int:
    """Seconds to wait before retrying: honor Telegram's `retry_after` when
    present (429), otherwise capped exponential backoff."""
    try:
        ra = json.loads(body).get("parameters", {}).get("retry_after")
        if ra:
            return int(ra) + 1
    except Exception:
        pass
    return min(2 ** attempt, 10)


def _api(token: str, method: str, payload: dict = None, *,
         clear_on_conflict: bool = True):
    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode() if payload else None
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "liquid-bot/1.0"},
        method="POST" if data else "GET",
    )
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code in _TRANSIENT_STATUS and attempt < MAX_RETRIES - 1:
                # On a conflict, actively release the update stream before retry.
                if e.code == 409 and clear_on_conflict:
                    _clear_conflicts(token)
                wait = _retry_after(body, attempt)
                print(f"[telegram] {method} HTTP {e.code}; nuovo tentativo tra {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Telegram {method} HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Telegram {method} failed after {MAX_RETRIES} attempts: {last_exc}")


def _clear_conflicts(token: str, drop_pending_updates: bool = True) -> None:
    """Best-effort recovery from HTTP 409. A 409 means another consumer — a
    leftover webhook, or a second polling process — is holding the bot's update
    stream. `deleteWebhook` removes the webhook case (and `drop_pending_updates`
    clears the stale backlog) so a fresh getUpdates long-poll can take over. It is
    harmless if no webhook is set, and safe to call repeatedly."""
    try:
        _api(token, "deleteWebhook", {"drop_pending_updates": drop_pending_updates},
             clear_on_conflict=False)
        action = "webhook/coda ripuliti" if drop_pending_updates else "webhook rimosso"
        print(f"[telegram] deleteWebhook OK ({action}).")
    except Exception as e:
        print(f"[telegram] deleteWebhook fallito (ignoro): {e}")


# ─── Low-level helpers ────────────────────────────────────────────────────────

def _do_send(token: str, chat_id: str, text: str) -> int:
    res = _api(token, "sendMessage", {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    })
    return res["result"]["message_id"]


def _latest_offset(token: str) -> int:
    res     = _api(token, "getUpdates", {"limit": 100, "timeout": 0})
    updates = res.get("result", [])
    return updates[-1]["update_id"] + 1 if updates else 0


# ─── Public API ───────────────────────────────────────────────────────────────

def send_message(text: str) -> None:
    """Send a plain text message (errors, status updates, etc.)."""
    token, chat_id = _creds()
    _do_send(token, chat_id, text)


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _sizing_lines(proposal: dict) -> list[str]:
    """Build the 'how much am I investing' block, if sizing info is available.

    Reads size_usd (notional), leverage, margin_usd, risk_usd, risk_pct. Derives
    margin from size_usd/leverage and risk from risk_pct*equity when not given.
    When sizing reduced the trade, target_risk_pct/target_risk_usd preserve the
    intended risk while risk_pct/risk_usd always describe the executable order.
    Returns [] when there is no size to show (graceful degradation)."""
    size_usd = proposal.get("size_usd", proposal.get("size"))
    if size_usd is None:
        return []
    try:
        size_usd = float(size_usd)
    except (TypeError, ValueError):
        return []

    lev = proposal.get("leverage") or 1
    margin = proposal.get("margin_usd")
    if margin is None:
        try:
            margin = size_usd / float(lev)
        except (TypeError, ValueError, ZeroDivisionError):
            margin = None

    risk_usd = proposal.get("risk_usd")
    if risk_usd is None and proposal.get("risk_pct") and proposal.get("equity"):
        try:
            risk_usd = float(proposal["risk_pct"]) * float(proposal["equity"])
        except (TypeError, ValueError):
            risk_usd = None

    risk_pct = proposal.get("risk_pct")
    risk_pct_str = f" ({float(risk_pct)*100:.1f}% equity)" if risk_pct else ""

    # These three are COMPUTED before execution (from the intended entry / sizing),
    # so they are estimates: the real filled values on Liquid differ (market-order
    # slippage, fees, size rounding). Labelled "(stima)" to avoid confusion.
    lines = ["", f"💰 *Investito (stima):* {_money(size_usd)}"]
    if margin is not None:
        lines.append(f"🔒 *Margine impegnato (stima):* {_money(margin)}")
    if risk_usd is not None:
        lines.append(f"⚠️ *Rischio se tocca SL (stima):* {_money(risk_usd)}{risk_pct_str}")
    if proposal.get("size_adjusted"):
        try:
            target_pct = float(proposal["target_risk_pct"])
            actual_pct = float(proposal["risk_pct"])
            retention = float(proposal.get("risk_retention", actual_pct / target_pct))
            target_usd = proposal.get("target_risk_usd")
            target_money = f" / {_money(float(target_usd))}" if target_usd is not None else ""
            lines.append(
                f"📉 *Size adattata:* rischio ideale {target_pct*100:.2f}%"
                f"{target_money} → effettivo {actual_pct*100:.2f}% "
                f"({retention*100:.1f}% conservato)"
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
    # Explain every leverage change so the approved tuple is transparent.
    if proposal.get("adjusted") and proposal.get("requested_leverage"):
        try:
            req = float(proposal["requested_leverage"])
            raw_lev = proposal.get("leverage")
            lev = req if raw_lev is None else float(raw_lev)
            if abs(lev - req) > 1e-9:
                motive = (
                    "per rientrare nel margine"
                    if lev > req
                    else "per rispettare il collaterale minimo"
                )
                lines.append(f"⚙️ *Leva adattata:* {req:g}x → {lev:g}x {motive}")
        except (TypeError, ValueError):
            pass
    return lines


def _markdown_text(value) -> str:
    # Messages are sent with legacy parse_mode="Markdown", which does NOT support
    # backslash escaping (only MarkdownV2 does). Backslash-escaping here produced
    # a literal "\" plus a still-active "_"/"*" entity delimiter — e.g.
    # "binance_futures" became "binance\_futures", opening an italic entity that
    # collided with the motivation italics and triggered a Telegram 400
    # "can't parse entities". For legacy Markdown the only safe move is to
    # neutralize the entity characters by replacing them with harmless lookalikes.
    return (
        str(value)
        .replace("_", "-")
        .replace("*", "-")
        .replace("`", "'")
        .replace("[", "(")
    )


def _execution_lines(proposal: dict) -> list[str]:
    context = proposal.get("market_context") or {}
    instrument = context.get("instrument") or {}
    if not context:
        return ["", "*Esecuzione:* NON RISOLTA"]
    if context.get("resolution_status") != "resolved":
        reasons = "; ".join(str(v) for v in context.get("reasons") or ())
        return ["", f"*Esecuzione:* {context.get('resolution_status', 'unknown')} - {reasons}"]
    displacement = context.get("dislocation_bps")
    displacement_text = (
        f"{float(displacement):+.2f} bps" if displacement is not None else "?"
    )
    execution_price = context.get("execution_price")
    signal_price = context.get("signal_price")
    return [
        "",
        f"*Venue segnale:* {_markdown_text(context.get('signal_venue') or '?')}",
        f"*Venue derivati:* {_markdown_text(context.get('derivatives_venue') or 'n/a')}",
        f"*Venue esecuzione:* {_markdown_text(context.get('execution_venue'))}",
        f"*Strumento:* {_markdown_text(instrument.get('instrument_id'))} "
        f"({_markdown_text(instrument.get('market_type') or 'gestito da Co-Invest')})",
        (
            f"*Prezzo usato:* {context.get('execution_price_type')} "
            f"{execution_price}"
        ),
        (
            f"*Prezzo segnale:* {context.get('signal_price_type')} {signal_price} "
            f"- dislocazione {displacement_text}"
        ),
    ]


def format_proposal(proposal: dict) -> str:
    """Build the full trade-proposal message (Markdown). Single source of truth so
    the sent message and the --test preview never diverge.

    Expected keys: asset, strategy, signal, timeframe, entry, target, stop_loss,
    leverage, confidence, risk_reward, motivation (optional). Sizing keys
    (size_usd, margin_usd, risk_usd, risk_pct, equity) are shown when present.
    """
    side_emoji = "🟢" if proposal.get("signal") == "long" else "🔴"
    rr   = proposal.get("risk_reward") or proposal.get("rr")
    rr_str = f"{rr:.2f}" if rr is not None else "—"
    conf_str = f"{round(proposal.get('confidence', 0) * 100, 1)}%"
    motivation = proposal.get("motivation") or proposal.get("reason") or ""
    strategy = str(proposal.get("strategy", "?")).replace("_", "-")

    lines = [
        f"🔔 *Liquid Bot — Nuova Proposta* {side_emoji}",
        "",
        f"*Strategia:*  {strategy}",
        f"*Asset:*      {proposal.get('asset', '?')}",
        f"*Side:*       {proposal.get('signal', '?').upper()}",
        f"*Timeframe:*  {proposal.get('timeframe', '?')}",
        "",
        f"*Entry (stima):* {proposal.get('entry', '?')}",
        f"*Take Profit:* {proposal.get('target', '?')}",
        f"*Stop Loss:*  {proposal.get('stop_loss', '?')}",
        f"*Leverage:*   {proposal.get('leverage', 1)}x",
    ]
    lines += _execution_lines(proposal)
    lines += _sizing_lines(proposal)
    lines += [
        "",
        f"*Confidence:* {conf_str}",
        f"*Risk/Reward:* {rr_str}",
    ]
    if motivation:
        # Motivation is free text wrapped in italic "_..._". A stray "_"/"*"/"`"
        # inside it (e.g. "vol_ratio") would open/close a spurious entity and
        # break legacy Markdown parsing, so neutralize the entity characters.
        safe_motivation = _markdown_text(motivation)
        lines += ["", f"_{safe_motivation}_"]
    lines += [
        "",
        "Rispondi *accetta* / *ok* / *yes* per approvare.",
        "Qualsiasi altra risposta o silenzio entro 30 min = rifiuto.",
    ]
    return "\n".join(lines)


def send_proposal(proposal: dict) -> None:
    """Send a formatted trade proposal message."""
    assert_executable_proposal(proposal)
    token, chat_id = _creds()
    _do_send(token, chat_id, format_proposal(proposal))


def wait_response(timeout_minutes: int = 30, start_offset: int = None) -> bool:
    """
    Poll for a user reply. Returns True if the user accepts, False on
    timeout or any other reply. Polls every ~30 seconds via long-polling.
    """
    token, chat_id = _creds()

    # Structural single-consumer handoff: if the persistent command poller
    # (telegram_bot.py) is running, claim the stream and give it time to yield
    # its in-flight long-poll before we start ours — this prevents the HTTP 409
    # collision by design rather than recovering from it. Best-effort: if the
    # lock module is unavailable, fall back to the original behavior.
    poller_offset = None
    if _lock is not None:
        try:
            _lock.acquire("wait_response")
            if _lock.poller_alive():
                print(f"[telegram] Poller /portfolio attivo — attendo lo yield "
                      f"({_lock.GRACE_SECONDS}s) per evitare il 409.")
                poller_offset = _lock.load_offset()
                time.sleep(_lock.GRACE_SECONDS)
        except Exception as e:
            print(f"[telegram] Coordinamento lock non riuscito (ignoro): {e}")

    try:
        # Proactively release the update stream before we start, so the very
        # first getUpdates does not hit HTTP 409 (stale webhook / leftover poller).
        _clear_conflicts(token, drop_pending_updates=False)

        # Resume from where the poller left off (so a reply arriving during the
        # handoff is not skipped); otherwise skip the stale backlog as before.
        if start_offset is not None:
            offset = start_offset
        elif poller_offset is not None:
            offset = poller_offset
        else:
            try:
                offset = _latest_offset(token)
            except Exception as e:
                print(f"[telegram] Offset iniziale non leggibile ({e}); parto da 0.")
                offset = 0
        deadline = time.time() + timeout_minutes * 60

        print(f"[telegram] In attesa di risposta (max {timeout_minutes} min)...")

        while time.time() < deadline:
            remaining  = int(deadline - time.time())
            poll_secs  = min(remaining, POLL_TIMEOUT)
            if poll_secs <= 0:
                break

            try:
                res = _api(token, "getUpdates", {
                    "offset":          offset,
                    "timeout":         poll_secs,
                    "allowed_updates": ["message"],
                }, clear_on_conflict=False)
            except Exception as e:
                msg = str(e)
                print(f"[telegram] Errore polling: {msg}. Riprovo...")
                # If the conflict persisted past the inner retries, clear again.
                if "409" in msg or "Conflict" in msg:
                    _clear_conflicts(token, drop_pending_updates=False)
                time.sleep(5)
                continue

            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                    continue
                text = msg.get("text", "").strip()
                word = text.lower().strip(".,!? ")
                if word in ACCEPT_WORDS:
                    print(f"[telegram] ACCETTATO (risposta: '{text}')")
                    _do_send(token, chat_id, "✅ Trade approvato. Esecuzione in corso...")
                    return True
                else:
                    print(f"[telegram] RIFIUTATO (risposta: '{text}')")
                    _do_send(token, chat_id, "❌ Trade rifiutato.")
                    return False

        print("[telegram] Timeout: nessuna risposta. Proposta annullata.")
        _do_send(token, chat_id, "⏰ Nessuna risposta ricevuta. Trade annullato.")
        return False
    finally:
        # Hand the stream back to the command poller on every exit path.
        if _lock is not None:
            try:
                _lock.release("wait_response")
            except Exception:
                pass


def notify_and_wait(proposal: dict, timeout_minutes: int = 30) -> bool:
    """Convenience: send_proposal + wait_response in one call."""
    assert_executable_proposal(proposal)
    token, chat_id = _creds()
    start_offset = None
    try:
        start_offset = _latest_offset(token)
    except Exception as e:
        print(f"[telegram] Offset pre-proposta non leggibile ({e}); uso handoff standard.")
    _do_send(token, chat_id, format_proposal(proposal))
    return wait_response(timeout_minutes, start_offset=start_offset)


# ─── CLI ──────────────────────────────────────────────────────────────────────

_TEST_PROPOSAL = {
    "strategy":    "momentum-trading",
    "asset":       "BTC",
    "signal":      "long",
    "timeframe":   "15m",
    "entry":       67_420.0,
    "target":      68_500.0,
    "stop_loss":   66_800.0,
    "leverage":    10,
    "confidence":  0.72,
    "risk_reward": 1.74,
    # Sizing (normalmente iniettato dalla routine prima della notifica):
    "equity":      10_000.0,
    "risk_pct":    0.04,
    "size_usd":    43_496.77,   # 400$ rischio / 620$ stop-dist × 67420 entry
    "margin_usd":  4_349.68,    # size_usd / 10x
    "risk_usd":    400.0,
    "motivation":  "Breakout sopra 20-day high con volume 1.4x — RSI 63, MACD positivo.",
    "market_context": {
        "signal_venue": "kraken",
        "derivatives_venue": "binance_futures",
        "execution_venue": "runtime-test",
        "resolution_status": "resolved",
        "signal_price": 67_420.0,
        "signal_price_type": "last",
        "execution_price": 67_420.0,
        "execution_price_type": "last",
        "execution_price_observed_at": datetime.now().astimezone().isoformat(),
        "execution_data_age_seconds": 0.0,
        "dislocation_bps": 0.0,
        "absolute_dislocation_bps": 0.0,
        "max_data_age_seconds": 30.0,
        "max_dislocation_bps": 25.0,
        "minimum_collateral_usd": 15.0,
        "live_trading_allowed": True,
        "instrument": {
            "execution_venue": "runtime-test",
            "instrument_id": "CATALOG_RUNTIME_ID",
            "base_asset": "BTC",
            "quote_asset": "USD",
            "market_type": "perpetual",
            "collateral_currency": "USD",
            "contract_multiplier": 1.0,
            "tick_size": 0.1,
            "lot_size": 0.001,
            "minimum_notional": 10.0,
            "maximum_leverage": 25.0,
            "mark_price": 67_420.0,
            "index_price": 67_420.0,
            "last_price": 67_420.0,
            "stop_trigger_type": "mark",
            "margin_mode": "cross",
        },
    },
}

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python telegram_notify.py <proposal.json>")
        print("       python telegram_notify.py --test")
        sys.exit(1)

    if sys.argv[1] == "--message":
        if len(sys.argv) < 3:
            print("Usage: python telegram_notify.py --message \"text\"")
            sys.exit(1)
        send_message(sys.argv[2])
        sys.exit(0)

    if sys.argv[1] == "--test":
        sys.stdout.reconfigure(encoding="utf-8")
        prop = _TEST_PROPOSAL
        print("=== MODALITÀ TEST — messaggio che verrà inviato ===")
        print(format_proposal(prop))  # same builder as the real send
        print("===================================================")

        _load_dotenv()  # honor credentials stored in .env, not just shell env
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat  = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()
        if token and chat:
            print("\nEnv vars trovate — invio messaggio reale a Telegram...")
            notify_and_wait(prop, timeout_minutes=2)
        else:
            print("\nTELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID non settate — solo anteprima locale.")
        sys.exit(0)

    with open(sys.argv[1]) as f:
        prop = json.load(f)

    approved = notify_and_wait(prop)
    sys.exit(0 if approved else 1)
