"""
telegram_notify.py
Sends a trade proposal to Telegram and waits for user approval.

Env vars required:
  TELEGRAM_BOT_TOKEN  — bot token from BotFather
  TELEGRAM_CHAT_ID    — target chat/user ID

Returns:
  True   if the user replies with "accetta", "si", "ok", or "yes"
  False  on any other reply or after WAIT_SECONDS timeout
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

WAIT_SECONDS   = 30 * 60   # 30 minutes
POLL_INTERVAL  = 5         # seconds between getUpdates calls
ACCEPT_WORDS   = {"accetta", "si", "sì", "ok", "yes"}

# ─── Telegram API ──────────────────────────────────────────────────────────────

def _api(token: str, method: str, payload: dict = None):
    url  = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode() if payload else None
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "liquid-bot/1.0"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {method} HTTP {e.code}: {e.read().decode()}") from e


def _send(token: str, chat_id: str, text: str) -> int:
    """Send a message; returns message_id."""
    res = _api(token, "sendMessage", {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    })
    return res["result"]["message_id"]


def _latest_offset(token: str) -> int:
    """Return offset = last update_id + 1, so old messages are skipped."""
    res = _api(token, "getUpdates", {"limit": 100, "timeout": 0})
    updates = res.get("result", [])
    if not updates:
        return 0
    return updates[-1]["update_id"] + 1


def _poll(token: str, chat_id: str, offset: int, deadline: float):
    """
    Long-poll getUpdates until deadline.
    Yields (text, new_offset) for each message from chat_id.
    """
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        long_poll = min(remaining, 20)   # Telegram max long-poll = 20s
        try:
            res = _api(token, "getUpdates", {
                "offset":          offset,
                "timeout":         long_poll,
                "allowed_updates": ["message"],
            })
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue

        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg    = upd.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) == str(chat_id):
                yield msg.get("text", "").strip(), offset

    return


# ─── Public API ────────────────────────────────────────────────────────────────

def build_message(proposal: dict) -> str:
    """Format a human-readable trade proposal message."""
    side_emoji = "🟢" if proposal.get("signal") == "long" else "🔴"
    lines = [
        f"*Liquid Trading Bot — Nuova Proposta* {side_emoji}",
        "",
        f"*Asset:*      {proposal.get('asset', '?')}",
        f"*Strategia:*  {proposal.get('strategy', '?')}",
        f"*Timeframe:*  {proposal.get('timeframe', '?')}",
        f"*Side:*       {proposal.get('signal', '?').upper()}",
        f"*Entry:*      {proposal.get('entry', '?')}",
        f"*Take Profit:* {proposal.get('target', '?')}",
        f"*Stop Loss:*  {proposal.get('stop_loss', '?')}",
        f"*Leverage:*   {proposal.get('leverage', 1)}x",
        f"*Confidence:* {round(proposal.get('confidence', 0) * 100, 1)}%",
        "",
        f"Rispondi *accetta* / *ok* / *yes* per approvare.",
        f"Qualsiasi altra risposta o silenzio entro 30 min = rifiuto.",
    ]
    return "\n".join(lines)


def notify_and_wait(proposal: dict) -> bool:
    """
    Send the trade proposal to Telegram and wait for approval.

    Args:
        proposal: dict with keys: asset, strategy, signal, timeframe,
                  entry, target, stop_loss, leverage, confidence.

    Returns:
        True  — user replied with an accept word
        False — rejected or timed out
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat_id:
        raise EnvironmentError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as environment variables."
        )

    offset   = _latest_offset(token)
    message  = build_message(proposal)
    deadline = time.time() + WAIT_SECONDS

    _send(token, chat_id, message)
    print(f"[telegram] Messaggio inviato. In attesa di risposta (max 30 min)...")

    for text, offset in _poll(token, chat_id, offset, deadline):
        word = text.lower().strip(".,!? ")
        if word in ACCEPT_WORDS:
            print(f"[telegram] Proposta ACCETTATA (risposta: '{text}')")
            _send(token, chat_id, "✅ Trade approvato. Esecuzione in corso...")
            return True
        else:
            print(f"[telegram] Proposta RIFIUTATA (risposta: '{text}')")
            _send(token, chat_id, "❌ Trade rifiutato.")
            return False

    print("[telegram] Timeout: nessuna risposta entro 30 minuti. Proposta annullata.")
    _send(token, chat_id, "⏰ Nessuna risposta ricevuta. Trade annullato.")
    return False


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python telegram_notify.py <proposal.json>")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        prop = json.load(f)

    approved = notify_and_wait(prop)
    sys.exit(0 if approved else 1)
