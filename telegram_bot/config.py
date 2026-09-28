"""Общая конфигурация: токен бота, доступ к PostgreSQL, параметры пайплайна.

Модуль импортируют и бот, и лоадер (`pipeline/`), и modeling, и тесты — поэтому
сам импорт не должен требовать токена или падать на дефолтах `PG_*`. Строгая
проверка обязательных переменных для бота — `validate_env()`, вызывается явно
из точек входа (`bot.py`, `push_digest_job.py`), а не на уровне импорта.
"""

import os
from datetime import date
from typing import Optional, Tuple

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Переменные, без которых боту (в отличие от лоадера) стартовать нельзя —
# см. validate_env(). SEASON_ID тоже входит: без него бот молча показал бы
# данные последнего дефолта вместо явного падения на старте (Задача 33).
_REQUIRED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "PG_HOST",
    "PG_PORT",
    "PG_USER",
    "PG_DATABASE",
    "SEASON_ID",
)


def validate_env() -> None:
    """Проверяет, что боту заданы все обязательные переменные окружения.

    Зачем: `TOKEN` и `PG_*` ниже по файлу тихо подставляют дефолты — это нужно
    лоадеру и тестам, у которых токена нет и быть не должно, но для самого
    бота отсутствующий токен, адрес БД или сезон должен быть падением на
    старте, а не поздней рантайм-ошибкой у пользователя. Смотрит на «сырое»
    окружение (`os.environ`), а не на уже задефолченные `PG_*` ниже —
    иначе отсутствие переменной было бы неотличимо от дефолта.

    Raises:
        RuntimeError: названа первая отсутствующая или пустая переменная из
            `TELEGRAM_BOT_TOKEN`/`PG_HOST`/`PG_PORT`/`PG_USER`/`PG_DATABASE`/
            `SEASON_ID`. Значение переменной в сообщение не попадает.
    """
    for name in _REQUIRED_ENV_VARS:
        if not os.getenv(name, "").strip():
            raise RuntimeError(
                f"Не задана обязательная переменная окружения: {name}. "
                "Задайте её в .env или экспортируйте в shell."
            )


def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value if value else default


PG_PORT = _env("PG_PORT", "5432")
PG_HOST = _env("PG_HOST", "localhost")
PG_USER = _env("PG_USER", "postgres")
PG_DATABASE = _env("PG_DATABASE", "postgres")


def derive_season(season_id: int) -> Tuple[str, str]:
    """Выводит из `SEASON_ID` то, что раньше задавалось отдельными переменными.

    Зачем: `SEASON_ID` (вида `20262027`) — единственный источник сезона
    (Задача 33); подпись в сообщениях бота и дата старта полной перезагрузки
    (`make season-load-full`) читаются из него здесь, в одном месте, вместо
    двух независимых переменных окружения (`CURRENT_SEASON`, `SEASON_START`),
    которые могли разъехаться друг с другом и с `SEASON_ID`.

    Дата старта — всегда 1 сентября первого года `SEASON_ID`: окно шире
    реального старта регулярного чемпионата (обычно начало-середина октября),
    но это безвредно — `fetch_final_games()` фильтрует игры одним
    date-filtered запросом к API (`load_season_modern.py`), а не построчным
    обходом дней, так что пустые дни в начале окна не стоят лишних запросов.

    Args:
        season_id: NHL `seasonId`, например `20262027`.

    Returns:
        Пара `(current_season, date_from)`: `("26/27", "2026-09-01")`.
    """
    start_year, end_year = season_id // 10000, season_id % 10000
    current_season = f"{start_year % 100:02d}/{end_year % 100:02d}"
    date_from = date(start_year, 9, 1).isoformat()
    return current_season, date_from


def _season_id_from_env() -> Optional[int]:
    """Читает `SEASON_ID` из окружения без требования, что он задан.

    Зачем: `config.py` импортируют тесты и CI без `.env` (см. шапку модуля),
    поэтому отсутствие `SEASON_ID` не должно валить импорт — падать на этом
    обязаны только явные точки входа (`validate_env()` для бота, CLI лоадера
    в `pipeline/load_season_modern.py`), а не сам факт импорта модуля.

    Raises:
        ValueError: значение задано, но не целое число — эта ошибка, в
            отличие от отсутствия переменной, видна сразу при импорте.
    """
    raw = os.getenv("SEASON_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            "Некорректное значение переменной окружения SEASON_ID: ожидается целое число"
        ) from None


# Pipeline settings are centralized here too.
SEASON_ID: Optional[int] = _season_id_from_env()
CURRENT_SEASON: Optional[str] = derive_season(SEASON_ID)[0] if SEASON_ID is not None else None

# Нет дефолта: раньше "пустая DATE_FROM -> дата старта SEASON_ID на момент
# импорта config.py" молча расходилась с сезоном после переопределения
# `--season-id` в лоадере (Задача 33, fix round 1 — GC4). Дату старта
# окна для того SEASON_ID, с которым лоадер реально запускается,
# `pipeline/load_season_modern.py::main()` выводит через `derive_season()`
# сам, уже после применения `--season-id`.
DATE_FROM: Optional[str] = os.getenv("DATE_FROM", "").strip() or None
DATE_TO = _env("DATE_TO", date.today().isoformat())


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(name: str, default: float) -> float:
    """Отсутствующая/пустая переменная — `default`; кривое значение — `ValueError`
    с именем переменной, без её значения."""
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
