"""
test_telegram_409.py
Proves the /portfolio poller and the proposal-approval wait cannot collide on
HTTP 409, structurally.

Three parts:
  1. OFFLINE  — deterministic checks of the telegram_lock state machine.
  2. CONTROL  — REAL Telegram: two concurrent getUpdates on the same token,
                showing the 409 hazard actually exists (baseline).
  3. FIXED    — REAL Telegram: a poller that respects the lock + a wait_response
                that acquires it. Assert ZERO 409 across the handoff.

getUpdates is read-only (it never sends anything), so running this does not
touch the account or post messages. Run: python test_telegram_409.py
"""

import json
import threading
import time
import urllib.error
import urllib.request

import telegram_lock as lock
from telegram_notify import _creds, _clear_conflicts

PASS = "✅"
FAIL = "❌"
_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def raw_getupdates(token: str, timeout: int = 5, offset=None):
    """One raw getUpdates. Returns (status_code, short_body). 200 => ok."""
    payload = {"timeout": timeout}
    if offset is not None:
        payload["offset"] = offset
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            r.read()
            return 200, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:100]
    except Exception as e:  # network hiccup
        return -1, str(e)[:100]


# ─── Part 1: offline state machine ────────────────────────────────────────────

def test_offline():
    print("\n[1] Lock state machine (offline, deterministic)")
    lock.release("test");
    try:
        lock.LOCK_PATH.unlink(missing_ok=True)
        lock.OFFSET_PATH.unlink(missing_ok=True)
    except Exception:
        pass

    check("stream libero all'inizio", not lock.is_held())
    check("nessun poller senza heartbeat", not lock.poller_alive())

    lock.acquire("wait_response")
    check("dopo acquire lo stream è occupato", lock.is_held())

    lock.save_offset(4242)
    check("poller_alive dopo heartbeat", lock.poller_alive())
    check("offset handoff round-trip", lock.load_offset() == 4242)

    lock.release("wait_response")
    check("dopo release lo stream è libero", not lock.is_held())

    # Stale lock must not block forever.
    lock.LOCK_PATH.write_text(json.dumps(
        {"owner": "ghost", "pid": 1, "ts": time.time() - lock.STALE_SECONDS - 10}))
    check("un lock stale è considerato libero", not lock.is_held())
    lock.LOCK_PATH.unlink(missing_ok=True)

    # A foreign live lock must NOT be releasable by the wrong owner.
    lock.acquire("wait_response")
    lock.LOCK_PATH.write_text(json.dumps(
        {"owner": "someone_else", "pid": 999999, "ts": time.time()}))
    lock.release("wait_response")  # wrong owner+pid → should leave it
    check("release non tocca il lock di un altro owner", lock.is_held())
    lock.LOCK_PATH.unlink(missing_ok=True)
    lock.OFFSET_PATH.unlink(missing_ok=True)


# ─── Part 2: real 409 baseline ────────────────────────────────────────────────

def test_control_409(token):
    print("\n[2] CONTROL — due getUpdates concorrenti sullo stesso token (Telegram reale)")
    _clear_conflicts(token)
    results = {}

    def worker(name):
        results[name] = raw_getupdates(token, timeout=8)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = [results["A"][0], results["B"][0]]
    print(f"      esiti: A={results['A'][0]}  B={results['B'][0]}")
    check("il polling concorrente PRODUCE un 409 (hazard reale)", 409 in codes,
          "se non appare, ripeti: dipende dalla sovrapposizione temporale")
    _clear_conflicts(token)


# ─── Part 3: coordinated, no 409 ──────────────────────────────────────────────

def test_fixed_no_409(token):
    print("\n[3] FIXED — poller che rispetta il lock + wait_response (Telegram reale)")
    _clear_conflicts(token)
    try:
        lock.LOCK_PATH.unlink(missing_ok=True)
        lock.OFFSET_PATH.unlink(missing_ok=True)
    except Exception:
        pass

    stop = threading.Event()
    conflicts = {"poller": 0, "waiter": 0}
    poller_polls = {"n": 0}

    def poller():
        off = lock.load_offset()
        lock.save_offset(off or 0)  # heartbeat before first (slow) poll
        while not stop.is_set():
            if lock.is_held():
                time.sleep(1)  # yield: do not touch the stream
                continue
            code, _ = raw_getupdates(token, timeout=lock.POLLER_POLL_TIMEOUT, offset=off)
            poller_polls["n"] += 1
            if code == 409:
                conflicts["poller"] += 1
            lock.save_offset(off or 0)  # heartbeat

    p = threading.Thread(target=poller)
    p.start()
    time.sleep(1.5)  # let the poller heartbeat and enter its poll

    # ---- wait_response side: the exact coordination wait_response performs ----
    lock.acquire("wait_response")
    if lock.poller_alive():
        print(f"      poller vivo — attendo GRACE={lock.GRACE_SECONDS}s per lo yield")
        time.sleep(lock.GRACE_SECONDS)
    _clear_conflicts(token)
    for _ in range(3):
        code, _ = raw_getupdates(token, timeout=4)
        if code == 409:
            conflicts["waiter"] += 1
    lock.release("wait_response")

    stop.set()
    p.join(timeout=lock.POLLER_POLL_TIMEOUT + 5)

    print(f"      poll del poller: {poller_polls['n']}  |  409 poller={conflicts['poller']}  409 waiter={conflicts['waiter']}")
    check("il poller ha effettivamente pollato prima dell'handoff", poller_polls["n"] >= 1)
    check("ZERO 409 sul lato wait_response", conflicts["waiter"] == 0)
    check("ZERO 409 sul lato poller", conflicts["poller"] == 0)

    lock.LOCK_PATH.unlink(missing_ok=True)
    lock.OFFSET_PATH.unlink(missing_ok=True)
    _clear_conflicts(token)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    test_offline()

    token, _chat = _creds()
    test_control_409(token)
    test_fixed_no_409(token)

    print("\n" + ("=" * 50))
    if _failures:
        print(f"{FAIL} FALLITI: {', '.join(_failures)}")
        sys.exit(1)
    print(f"{PASS} Tutti i controlli superati — 409 evitato strutturalmente.")
