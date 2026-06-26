"""
telegram_notify.py
Sends trade proposals to Telegram and waits for user approval.

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

def _api(token: str, method: str, payload: dict = None):
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
            raise RuntimeError(f"Telegram {method} HTTP {e.code}: {e.read().decode()}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Telegram {method} failed after {MAX_RETRIES} attempts: {last_exc}")


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


def send_proposal(proposal: dict) -> None:
    """
    Send a formatted trade proposal message.

    Expected proposal keys: asset, strategy, signal, timeframe,
    entry, target, stop_loss, leverage, confidence, risk_reward,
    motivation (optional 1-line reason).
    """
    token, chat_id = _creds()
    side_emoji = "🟢" if proposal.get("signal") == "long" else "🔴"
    rr   = proposal.get("risk_reward") or proposal.get("rr")
    rr_str = f"{rr:.2f}" if rr is not None else "—"
    conf_str = f"{round(proposal.get('confidence', 0) * 100, 1)}%"
    motivation = proposal.get("motivation") or proposal.get("reason") or ""

    lines = [
        f"🔔 *Liquid Bot — Nuova Proposta* {side_emoji}",
        "",
        f"*Strategia:*  {proposal.get('strategy', '?')}",
        f"*Asset:*      {proposal.get('asset', '?')}",
        f"*Side:*       {proposal.get('signal', '?').upper()}",
        f"*Timeframe:*  {proposal.get('timeframe', '?')}",
        "",
        f"*Entry:*      {proposal.get('entry', '?')}",
        f"*Take Profit:* {proposal.get('target', '?')}",
        f"*Stop Loss:*  {proposal.get('stop_loss', '?')}",
        f"*Leverage:*   {proposal.get('leverage', 1)}x",
        "",
        f"*Confidence:* {conf_str}",
        f"*Risk/Reward:* {rr_str}",
    ]
    if motivation:
        lines += ["", f"_{motivation}_"]
    lines += [
        "",
        "Rispondi *accetta* / *ok* / *yes* per approvare.",
        "Qualsiasi altra risposta o silenzio entro 30 min = rifiuto.",
    ]
    _do_send(token, chat_id, "\n".join(lines))


def wait_response(timeout_minutes: int = 30) -> bool:
    """
    Poll for a user reply. Returns True if the user accepts, False on
    timeout or any other reply. Polls every ~30 seconds via long-polling.
    """
    token, chat_id = _creds()
    offset   = _latest_offset(token)
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
            })
        except Exception as e:
            print(f"[telegram] Errore polling: {e}. Riprovo...")
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


def notify_and_wait(proposal: dict, timeout_minutes: int = 30) -> bool:
    """Convenience: send_proposal + wait_response in one call."""
    send_proposal(proposal)
    return wait_response(timeout_minutes)


# ─── CLI ──────────────────────────────────────────────────────────────────────

_TEST_PROPOSAL = {
    "strategy":    "momentum-trading",
    "asset":       "BTC",
    "signal":      "long",
    "timeframe":   "1h",
    "entry":       67_420.0,
    "target":      71_000.0,
    "stop_loss":   65_800.0,
    "leverage":    2,
    "confidence":  0.72,
    "risk_reward": 2.21,
    "motivation":  "Breakout sopra 20-day high con volume 1.4x — RSI 63, MACD positivo.",
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
        # Build and print the message locally without sending
        side_emoji = "🟢" if prop.get("signal") == "long" else "🔴"
        rr     = prop.get("risk_reward")
        rr_str = f"{rr:.2f}" if rr is not None else "—"
        conf   = f"{round(prop.get('confidence', 0) * 100, 1)}%"
        mot    = prop.get("motivation", "")
        lines  = [
            f"🔔 Liquid Bot — Nuova Proposta {side_emoji}",
            f"Strategia:   {prop['strategy']}",
            f"Asset:       {prop['asset']}",
            f"Side:        {prop['signal'].upper()}",
            f"Timeframe:   {prop['timeframe']}",
            f"Entry:       {prop['entry']}",
            f"Take Profit: {prop['target']}",
            f"Stop Loss:   {prop['stop_loss']}",
            f"Leverage:    {prop['leverage']}x",
            f"Confidence:  {conf}",
            f"Risk/Reward: {rr_str}",
        ]
        if mot:
            lines.append(f"Motivazione: {mot}")
        print("\n".join(lines))
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
