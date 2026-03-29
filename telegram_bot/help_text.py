"""Короткие тексты для /start и /help (фаза 1 UX)."""

START_MESSAGE = (
    "Привет! Я бот со статистикой NHL: результаты дня, турнирная таблица, "
    "лидеры сезона и подробные отчёты через меню.\n\n"
    "Список команд — в /help. Быстрый старт: /today, /table, /leaders, "
    "/game (карточка матча), /advanced, /shottypes."
)

_COMMAND_LINES = (
    "/today или /day — дайджест последнего игрового дня в базе (несколько матчей — сводка и кнопки «Матч N»)\n"
    "/table или /standings — турнирная таблица по дивизионам\n"
    "/leaders — топ-5 по очкам и голам (есть кнопка «Топ-10»)\n"
    "/game <id> — карточка матча; deep-link: /start game\\_<id>\n"
    "/advanced — SAT %, USAT %, GF %, OZ Start %, Shootout %\n"
    "/shottypes — голы по типам броска (кистевой, щелчок и т.д.)\n"
    "/stats — полное меню статистики с кнопками\n"
    "/cancel — выйти из сценария /stats\n"
    "/help — эта справка"
)

HELP_MESSAGE = f"{START_MESSAGE}\n\n*Команды*\n{_COMMAND_LINES}"
