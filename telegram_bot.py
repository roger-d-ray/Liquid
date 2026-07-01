"""
telegram_bot.py
Persistent Telegram command listener for the Liquid trading bot.

The rest of the bot is a set of one-shot scripts; this is the one long-running
process that stays connected and answers on-demand commands. Today it serves a
single read-only command:

  /portfolio  → read data/portfolio_state.json and reply with a readable
                summary (equity, available balance, used margin, open positions
                with entry price, PnL and leverage).

It is strictly READ-ONLY: it never calls execute_order() or touches the trading
flow (STEP 0-6). The snapshot it reads is populated by the Co-Invest MCP
assistant (get_portfolio) during the 60-min routine.

⚠️  Single-consumer constraint: Telegram allows only ONE getUpdates consumer per
bot token. Do NOT run this poller at the same time as telegram_notify.py's
wait_response() on the same TELEGRAM_BOT_TOKEN, or both will hit HTTP 409.
Either run them against separate bots, or pause this poller while a proposal is
awaiting approval.

Env vars required (read from environment or .env, never hard-coded):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Usage:
  python telegram_bot.py            # run the listener (Ctrl-C to stop)
  python telegram_bot.py --once     # process one poll cycle and exit (testing)
"""

import sys
import time
import traceback

# Reuse the hardened Telegram plumbing already in the repo.
from telegram_notify import (
    _api,
    _clear_conflicts,
    _creds,
    _do_send,
    _latest_offset,
    send_message,
)
from portfolio import PortfolioUnavailable, build_portfolio_message
import telegram_lock as lock

# Short long-poll so the poller can yield the stream quickly when a proposal
# wait (telegram_notify.wait_response) needs it — see telegram_lock.py.
POLL_TIMEOUT = lock.POLLER_POLL_TIMEOUT

_HELP_TEXT = (
    "🤖 *Liquid Bot — comandi*\n\n"
    "/portfolio — mostra saldo, equity, margine e posizioni aperte (sola lettura)\n"
    "/help — questo messaggio"
)


# ─── Command handlers ─────────────────────────────────────────────────────────

def _handle_portfolio(token: str, chat_id: str) -> None:
    """Reply with the formatted portfolio, or the error detail on failure."""
    try:
        msg = build_portfolio_message()
    except PortfolioUnavailable as e:
        _do_send(token, chat_id, f"⚠️ Portafoglio non disponibile: {e}")
        return
    except Exception as e:  # defensive: never let a handler crash the loop
        _do_send(token, chat_id, f"❌ Errore nel recupero del portafoglio: {e}")
        traceback.print_exc()
        return
    _do_send(token, chat_id, msg)


def _dispatch(token: str, chat_id: str, text: str) -> None:
    # Normalise: "/portfolio@my_bot arg" -> "portfolio"
    cmd = text.strip().split()[0].lstrip("/").split("@")[0].lower()
    if cmd == "portfolio":
        print("[bot] comando /portfolio")
        _handle_portfolio(token, chat_id)
    elif cmd in ("start", "help"):
        _do_send(token, chat_id, _HELP_TEXT)
    else:
        _do_send(token, chat_id, f"Comando non riconosciuto: `{text.strip()}`\n\n{_HELP_TEXT}")


# ─── Poll loop ────────────────────────────────────────────────────────────────

def _process_updates(token: str, chat_id: str, offset: int, poll_secs: int) -> int:
    """One getUpdates cycle. Returns the next offset."""
    res = _api(token, "getUpdates", {
        "offset":          offset,
        "timeout":         poll_secs,
        "allowed_updates": ["message"],
    })
    for upd in res.get("result", []):
        offset = upd["update_id"] + 1
        msg = upd.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue  # ignore anyone but the owner
        text = msg.get("text", "")
        if not text:
            continue
        try:
            _dispatch(token, chat_id, text)
        except Exception as e:
            # A failure handling one message must not kill the listener.
            print(f"[bot] errore nel dispatch di '{text}': {e}")
            traceback.print_exc()
            try:
                _do_send(token, chat_id, f"❌ Errore interno: {e}")
            except Exception:
                pass
    return offset


def run(once: bool = False) -> None:
    token, chat_id = _creds()

    # Take over the update stream cleanly (drop any leftover webhook/backlog),
    # then start after the current backlog so we don't replay stale commands.
    _clear_conflicts(token)
    try:
        offset = _latest_offset(token)
    except Exception as e:
        print(f"[bot] offset iniziale non leggibile ({e}); parto da 0.")
        offset = 0

    print(f"[bot] Listener avviato. In ascolto di comandi (chat {chat_id})...")
    lock.save_offset(offset)  # first heartbeat so wait_response can see us
    yielded = False

    while True:
        # Structural 409 avoidance: while a proposal wait owns the stream,
        # suspend polling entirely instead of colliding on getUpdates.
        if lock.is_held():
            if not yielded:
                print("[bot] Stream occupato da una proposta — sospendo il polling.")
                yielded = True
            time.sleep(2)
            if once:
                return
            continue
        if yielded:
            print("[bot] Stream libero — riprendo il polling.")
            yielded = False
            _clear_conflicts(token)  # clean re-take after the handoff

        try:
            offset = _process_updates(token, chat_id, offset, POLL_TIMEOUT)
            lock.save_offset(offset)  # heartbeat + offset handoff point
        except KeyboardInterrupt:
            print("\n[bot] Interrotto dall'utente. Chiusura.")
            return
        except Exception as e:
            msg = str(e)
            print(f"[bot] Errore polling: {msg}. Riprovo...")
            if "409" in msg or "Conflict" in msg:
                _clear_conflicts(token)
            time.sleep(5)
        if once:
            return


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        run(once="--once" in sys.argv)
    except KeyboardInterrupt:
        print("\n[bot] Chiusura.")
    except EnvironmentError as e:
        print(f"[bot] Config mancante: {e}")
        sys.exit(1)
