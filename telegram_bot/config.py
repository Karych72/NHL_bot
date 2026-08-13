"""Общая конфигурация: токен бота, доступ к PostgreSQL, параметры пайплайна.

Модуль импортируют и бот, и лоадер (`pipeline/`), и modeling, и тесты — поэтому
сам импорт не должен требовать токена или падать на дефолтах `PG_*`. Строгая
проверка обязательных переменных для бота — `validate_env()`, вызывается явно
из точек входа (`bot.py`, `push_digest_job.py`), а не на уровне импорта.
"""

import os
import getpass
from datetime import date

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Переменные, без которых боту (в отличие от лоадера) стартовать нельзя —
# см. validate_env().
_REQUIRED_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "PG_HOST", "PG_PORT", "PG_USER", "PG_DATABASE")


def validate_env() -> None:
    """Проверяет, что боту заданы все обязательные переменные окружения.

    Зачем: `TOKEN` и `PG_*` ниже по файлу тихо подставляют дефолты — это нужно
    лоадеру и тестам, у которых токена нет и быть не должно, но для самого
    бота отсутствующий токен или адрес БД должен быть падением на старте,
    а не поздней рантайм-ошибкой у пользователя. Смотрит на «сырое»
    окружение (`os.environ`), а не на уже задефолченные `PG_*` ниже —
    иначе отсутствие переменной было бы неотличимо от дефолта.

    Raises:
        RuntimeError: названа первая отсутствующая или пустая переменная из
            `TELEGRAM_BOT_TOKEN`/`PG_HOST`/`PG_PORT`/`PG_USER`/`PG_DATABASE`.
            Значение переменной в сообщение не попадает.
    """
    for name in _REQUIRED_ENV_VARS:
        if not os.getenv(name, "").strip():
            raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")


def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Некорректное значение переменной окружения {name}: ожидается целое число"
        ) from None


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
        raise ValueError(
            f"Некорректное значение переменной окружения {name}: ожидается число"
        ) from None


ENABLE_PUSH_DIGEST = _env_bool("ENABLE_PUSH_DIGEST", False)
PUSH_SEND_INTERVAL_SEC = _env_float("PUSH_SEND_INTERVAL_SEC", 0.05)
