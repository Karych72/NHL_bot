import os
import getpass

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value if value else default


PG_PORT = _env("PG_PORT", "5432")
PG_HOST = _env("PG_HOST", "localhost")
PG_USER = _env("PG_USER", getpass.getuser())
PG_DATABASE = _env("PG_DATABASE", "postgres")
