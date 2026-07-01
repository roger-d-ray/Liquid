"""
paper_reset.py
File-flag bridge for the "reset paper trading" Telegram command.

WHY A FLAG FILE
---------------
telegram_bot.py (the persistent command listener) has NO exchange/MCP
credentials — like /portfolio it can only touch local files. The actual reset is
an MCP action (reset_paper_account) that ONLY the routine agent can perform. So
the bot records the *intent* here after the user confirms on Telegram, and the
next 60-min routine run consumes it (STEP 0), executes the reset via MCP, sends a
Telegram confirmation, and clears the flag.

The flag lives under data/ (git-ignored), so it never leaves the machine.

API:
  request_reset(requested_by, note)  → write the pending-reset flag
  pending()                          → dict if a reset is queued, else None
  clear()                            → remove the flag (call after executing)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REQUEST_PATH = Path(__file__).parent / "data" / "reset_request.json"


def request_reset(requested_by: str = "telegram", note: str = "") -> dict:
    """Persist a pending-reset request. Overwrites any earlier one (idempotent)."""
    REQUEST_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "requested_by": requested_by,
        "note":         note,
    }
    REQUEST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def pending() -> Optional[dict]:
    """Return the queued reset request, or None if there isn't one."""
    if not REQUEST_PATH.exists():
        return None
    try:
        return json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt flag still means "a reset was requested" — surface it as such
        # so the agent resets and clears it, rather than silently ignoring.
        return {"requested_at": None, "requested_by": "unknown", "note": "flag illeggibile"}


def clear() -> bool:
    """Remove the pending-reset flag. Returns True if a flag was removed."""
    try:
        REQUEST_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


if __name__ == "__main__":
    import sys
    if "--request" in sys.argv:
        print("richiesta reset scritta:", request_reset(requested_by="cli"))
    elif "--clear" in sys.argv:
        print("flag rimosso:", clear())
    else:
        print("pending:", pending())
