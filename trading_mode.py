"""
Central trading-mode guardrails.

The Liquid account is reached through Co-Invest MCP, not through API keys stored
in this repository. This module does not switch Co-Invest by itself; it makes
the local routine declare its intent before any account-changing MCP action is
allowed.

Required for live execution:
  TRADING_MODE=live
  LIVE_TRADING_ALLOWED=true

Default is fail-closed: missing configuration means paper mode.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ENV_PATH = Path(__file__).parent / ".env"
VALID_MODES = {"paper", "live"}
TRUE_VALUES = {"1", "true", "yes", "y", "on"}


class TradingModeError(RuntimeError):
    """Raised when an account-changing action is blocked by mode settings."""


def _load_dotenv(path: Path = ENV_PATH) -> None:
    """Load simple KEY=VALUE lines from .env without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def trading_mode() -> str:
    """Return paper or live. Invalid values block rather than guessing."""
    _load_dotenv()
    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    if mode not in VALID_MODES:
        raise TradingModeError(
            f"TRADING_MODE non valido: {mode!r}. Usa 'paper' oppure 'live'."
        )
    return mode


def live_trading_allowed() -> bool:
    """Second live switch: must be explicitly true before live actions run."""
    _load_dotenv()
    value = os.environ.get("LIVE_TRADING_ALLOWED", "").strip().lower()
    return value in TRUE_VALUES


def is_live_mode() -> bool:
    return trading_mode() == "live"


def require_live_trading_enabled(action: str = "azione live") -> bool:
    """Block an account-changing live action unless both live switches are set."""
    mode = trading_mode()
    if mode != "live":
        raise TradingModeError(
            f"{action} bloccata: TRADING_MODE={mode}. "
            "Imposta TRADING_MODE=live dopo aver disattivato il paper su Co-Invest."
        )
    if not live_trading_allowed():
        raise TradingModeError(
            f"{action} bloccata: LIVE_TRADING_ALLOWED non e' true. "
            "Imposta LIVE_TRADING_ALLOWED=true solo quando vuoi usare soldi reali."
        )
    return True


def require_paper_trading_enabled(action: str = "azione paper") -> bool:
    """Block paper-only actions while the local routine is in live mode."""
    mode = trading_mode()
    if mode != "paper":
        raise TradingModeError(
            f"{action} bloccata: TRADING_MODE={mode}. "
            "Il reset paper e' disabilitato quando la routine e' in live."
        )
    return True


def status() -> dict:
    mode = trading_mode()
    return {
        "trading_mode": mode,
        "live_trading_allowed": live_trading_allowed(),
        "live_actions_enabled": mode == "live" and live_trading_allowed(),
    }


def main() -> int:
    if "--require-live" in sys.argv:
        require_live_trading_enabled("pre-flight ordine Co-Invest")
        print("OK: live trading abilitato localmente.")
        return 0
    if "--require-paper" in sys.argv:
        require_paper_trading_enabled("pre-flight reset paper")
        print("OK: paper trading abilitato localmente.")
        return 0
    print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TradingModeError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(2)
