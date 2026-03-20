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
