import os
import getpass
from datetime import date

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


PG_PORT = _env("PG_PORT", "5432")
PG_HOST = _env("PG_HOST", "localhost")
PG_USER = _env("PG_USER", getpass.getuser())
PG_DATABASE = _env("PG_DATABASE", "postgres")

# Pipeline settings are centralized here too.
DATE_FROM = _env("DATE_FROM", "2025-10-01")
DATE_TO = _env("DATE_TO", date.today().isoformat())
SEASON_ID = _env_int("SEASON_ID", 20252026)
CURRENT_SEASON = _env("CURRENT_SEASON", "25/26")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


ENABLE_PUSH_DIGEST = _env_bool("ENABLE_PUSH_DIGEST", False)
PUSH_SEND_INTERVAL_SEC = _env_float("PUSH_SEND_INTERVAL_SEC", 0.05)
