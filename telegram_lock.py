"""
telegram_lock.py
Cooperative single-consumer coordination for the Telegram update stream.

Telegram allows only ONE getUpdates long-poll in flight per bot token; two
overlapping polls collide with HTTP 409 (Conflict). Instead of relying on the
409 auto-recovery in telegram_notify.py, this module makes the mutual exclusion
*structural*: a filesystem lock guarantees that the persistent command poller
(telegram_bot.py) and the proposal-approval wait (telegram_notify.wait_response)
never poll at the same time.

Protocol
--------
- The command poller runs continuously. Before every getUpdates it checks
  `is_held()`; while the lock is held it SUSPENDS polling (does not touch the
  stream) and yields to the other consumer. After each poll it heartbeats its
  next offset via `save_offset()`.
- wait_response, before it starts polling, calls `acquire()`. If a poller is
  alive (`poller_alive()`), it waits GRACE_SECONDS — long enough for the poller
  to finish its current in-flight long-poll and yield — then it owns the stream
  exclusively. It resumes from the poller's last offset (`load_offset()`) so a
  reply that arrives during the handoff is not skipped. On exit it `release()`s.

Stale locks (from a crashed process) auto-expire after STALE_SECONDS so the
poller is never blocked forever.

No secrets here — only local coordination files under data/ (gitignored).
"""

import json
import os
import time
from pathlib import Path

_DATA_DIR    = Path(__file__).parent / "data"
LOCK_PATH    = _DATA_DIR / "telegram_stream.lock"
OFFSET_PATH  = _DATA_DIR / "telegram_offset.json"

# The command poller uses a short long-poll so it can yield quickly when the
# lock appears. GRACE must be >= this, so wait_response can be sure the poller's
# last in-flight poll has returned before it starts its own.
POLLER_POLL_TIMEOUT = 10          # seconds per poller getUpdates long-poll
GRACE_SECONDS       = POLLER_POLL_TIMEOUT + 3

STALE_SECONDS   = 40 * 60         # a wait_response never runs longer than this
ALIVE_SECONDS   = 30              # offset heartbeat fresher than this => poller live


def _now() -> float:
    return time.time()


# ─── Lock ─────────────────────────────────────────────────────────────────────

def is_held() -> bool:
    """True if a non-stale lock exists (someone else owns the stream)."""
    if not LOCK_PATH.exists():
        return False
    try:
        info = json.loads(LOCK_PATH.read_text())
        if _now() - float(info.get("ts", 0)) > STALE_SECONDS:
            return False  # stale => treat as free
        return True
    except Exception:
        # Unreadable lock: fall back to mtime, else treat as free.
        try:
            return (_now() - LOCK_PATH.stat().st_mtime) <= STALE_SECONDS
        except OSError:
            return False


def acquire(owner: str) -> None:
    """Claim the stream. Overwrites a stale lock."""
    _DATA_DIR.mkdir(exist_ok=True)
    LOCK_PATH.write_text(json.dumps({
        "owner": owner, "pid": os.getpid(), "ts": _now(),
    }))


def release(owner: str | None = None) -> None:
    """Release the stream. Best-effort; only clears our own lock if owner given."""
    try:
        if owner is not None and LOCK_PATH.exists():
            info = json.loads(LOCK_PATH.read_text())
            if info.get("owner") != owner or info.get("pid") != os.getpid():
                return  # not ours; leave it
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


# ─── Offset heartbeat / handoff ───────────────────────────────────────────────

def save_offset(offset: int) -> None:
    """Persist the poller's next offset (also serves as a liveness heartbeat)."""
    _DATA_DIR.mkdir(exist_ok=True)
    try:
        OFFSET_PATH.write_text(json.dumps({"offset": int(offset), "ts": _now()}))
    except Exception:
        pass


def load_offset() -> int | None:
    """Read the last offset the poller published, or None if unavailable."""
    if not OFFSET_PATH.exists():
        return None
    try:
        return int(json.loads(OFFSET_PATH.read_text())["offset"])
    except Exception:
        return None


def poller_alive() -> bool:
    """True if the command poller heartbeated its offset very recently."""
    if not OFFSET_PATH.exists():
        return False
    try:
        info = json.loads(OFFSET_PATH.read_text())
        return (_now() - float(info.get("ts", 0))) <= ALIVE_SECONDS
    except Exception:
        return False
