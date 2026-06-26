"""
git_push_log.py — commit & push of logs/proposals.jsonl (bot STEP 6).

Auth model (so the same command works locally and in the cloud):
  • If GITHUB_TOKEN is set  -> push over HTTPS to a token URL built at runtime.
    The token is NEVER written to git config, NEVER committed, NEVER printed.
    Use a fine-grained PAT scoped to this repo with Contents: read/write.
  • Otherwise (local dev)   -> plain `git push`, i.e. the configured remote
    (your SSH key). Nothing changes for local runs.

Env vars:
  GITHUB_TOKEN  — optional; fine-grained PAT (cloud only). May also live in .env.

Usage:
  python git_push_log.py
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO     = "roger-d-ray/Liquid"
LOG_PATH = "logs/proposals.jsonl"
BRANCH   = "main"


def _load_dotenv() -> None:
    """Mirror the other scripts: populate env from .env if not already set."""
    if os.environ.get("GITHUB_TOKEN"):
        return
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command. cmd is never allowed to contain the token (we log it)."""
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check)


def main() -> int:
    _load_dotenv()

    _run(["git", "config", "user.email", "bot@liquid.trade"])
    _run(["git", "config", "user.name", "Liquid Bot"])
    _run(["git", "add", LOG_PATH])

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    committed = _run(["git", "commit", "-m", f"bot: run {ts}"], check=False)
    if committed.returncode != 0:
        print("Nessuna modifica da committare (il log è invariato).")
        # We still try to push: there may be earlier commits not yet on the remote.

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        # Token URL is passed directly to push and never stored/logged.
        url = f"https://x-access-token:{token}@github.com/{REPO}.git"
        print(f"+ git push <token-url> HEAD:{BRANCH}   (token mascherato)")
        pushed = subprocess.run(["git", "push", url, f"HEAD:{BRANCH}"])
    else:
        print("GITHUB_TOKEN non impostato: uso il remote configurato (SSH locale).")
        pushed = _run(["git", "push"], check=False)

    if pushed.returncode != 0:
        print("ERRORE: git push fallito.", file=sys.stderr)
        return 1
    print("Push completato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
