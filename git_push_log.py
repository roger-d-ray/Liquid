"""
git_push_log.py
Commits logs/proposals.jsonl and pushes to the remote branch.
This file is the audit trail that proves each bot run occurred.
"""
import os
import subprocess
import sys
from pathlib import Path

BRANCH = "claude/gifted-johnson-k4zh86"
LOG_PATH = Path(__file__).parent / "logs" / "proposals.jsonl"


def run(cmd: list[str], check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def main():
    repo = Path(__file__).parent

    if not LOG_PATH.exists():
        print(f"[git_push_log] {LOG_PATH} non trovato — nulla da committare.")
        sys.exit(0)

    os.chdir(repo)

    # Stage the log file
    run(["git", "add", str(LOG_PATH)])

    # Check if there's anything staged
    status = run(["git", "diff", "--cached", "--name-only"], check=False)
    if not status.stdout.strip():
        print("[git_push_log] Nessuna modifica al log — skip commit.")
    else:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run(["git", "commit", "-m", f"bot: log run {ts}"])
        print(f"[git_push_log] Committed log at {ts}")

    # Push with retry (up to 4 attempts, exponential backoff)
    import time
    for attempt in range(4):
        result = run(["git", "push", "-u", "origin", BRANCH], check=False)
        if result.returncode == 0:
            print(f"[git_push_log] Push OK → origin/{BRANCH}")
            return
        print(f"[git_push_log] Push fallito (attempt {attempt+1}/4): {result.stderr.strip()}")
        if attempt < 3:
            wait = 2 ** (attempt + 1)
            print(f"[git_push_log] Riprovo tra {wait}s...")
            time.sleep(wait)

    print("[git_push_log] Push definitivamente fallito dopo 4 tentativi.")
    sys.exit(1)


if __name__ == "__main__":
    main()
