"""Заголовки для пагинируемых таблиц лидеров: ключ (таблица, колонка) → название в чате,
подписи колонки показателя в самой таблице и раскладка кнопок меню `/stats`, `/advanced`
и `/team_stats` — одно описание для меню, страниц и инструментов ИИ."""

from typing import Dict, List, Tuple

# Игроки / вратари — ключ (таблица БД, колонка сортировки)
PLAYER_STAT_TITLES: Dict[Tuple[str, str], str] = {
    ("players_season_stats", "points"): "Лучшие бомбардиры",
    ("players_season_stats", "goals"): "Лучшие снайперы",
    ("players_season_stats", "assists"): "Лучшие ассистенты",
    ("players_season_stats", "hits"): "Лидеры по хитам",
    ("players_season_stats", "plus_minus"): "Лидеры по показателю +-",
    ("players_season_stats", "pim"): "Лидеры по штрафным минутам",
    ("players_season_stats", "blocked"): "Лидеры по блокам",
    (
        "players_season_stats",
        "time_on_ice_per_game",
    ): "Лидеры по среднему игровому времени",
    ("players_season_stats", "shootout_pct"): "Лидеры по реализации буллитов (Shootout %)",
    ("players_advanced_stats", "sat_pct"): "Лидеры по Corsi (SAT %)",
    ("players_advanced_stats", "usat_pct"): "Лидеры по Fenwick (USAT %)",
    ("players_advanced_stats", "goals_pct"): "Лидеры по Goals For %",
    ("players_advanced_stats", "oz_start_pct"): "Лидеры по старту в зоне атаки (OZ Start %)",
    ("players_shot_types", "goals_wrist"): "Голы с кистевого",
    ("players_shot_types", "goals_slap"): "Голы щелчком",
    ("players_shot_types", "goals_snap"): "Голы с полуприёма",
    ("players_shot_types", "goals_backhand"): "Голы с бэкхенда",
    ("players_shot_types", "goals_tip_in"): "Голы направлением",
    ("players_shot_types", "goals_deflected"): "Голы отклонением",
    ("players_shot_types", "goals_wrap_around"): "Голы с за ворот",
    ("goalies_season_stats", "wins"): "Лидеры по победам",
    ("goalies_season_stats", "save_percentage"): "Лидеры по проценту отраженных бросков",
    ("goalies_season_stats", "shutouts"): "Лидеры по сухим матчам",
}

# /team_stats: ключ кнопки → (заголовок, колонки teams_stats). Одна колонка — рейтинг по ней,
# две — «за» и «против» в среднем за игру, рейтинг по их разнице.
TEAM_STAT_SPECS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "goals": ("Голы за и против в среднем за игру", ("goals_per_game", "goals_against_per_game")),
    "shots": ("Броски за и против в среднем за игру", ("shots_per_game", "shots_allowed")),
    "pp": ("Реализация большинства", ("power_play_percentage",)),
    "pk": ("Игра в меньшинстве", ("penalty_kill_percentage",)),
    "fo": ("Выигранные вбрасывания", ("face_off_win_percentage",)),
}

# Шапка колонки сортируемого показателя в таблице лидерборда — ключ колонка БД
# (игроков, вратарей и команд). Коротко: колонка rich-таблицы на телефоне узкая.
STAT_COLUMN_LABELS: Dict[str, str] = {
    "points": "Очки",
    "goals": "Голы",
    "assists": "Пасы",
    "hits": "Хиты",
    "plus_minus": "+/-",
    "pim": "Штраф",
    "blocked": "Блоки",
    "time_on_ice_per_game": "ВП",
    "shootout_pct": "Бул%",
    "sat_pct": "SAT%",
    "usat_pct": "USAT%",
    "goals_pct": "GF%",
    "oz_start_pct": "OZ%",
    "goals_wrist": "Голы",
    "goals_slap": "Голы",
    "goals_snap": "Голы",
    "goals_backhand": "Голы",
    "goals_tip_in": "Голы",
    "goals_deflected": "Голы",
    "goals_wrap_around": "Голы",
    "wins": "Побед",
    "save_percentage": "%ОБ",
    "shutouts": "Сух",
    "power_play_percentage": "%бол",
    "penalty_kill_percentage": "%мен",
    "face_off_win_percentage": "%вбр",
}

# Короткие ключи для /advanced → (таблица, колонка)
ADV_STANDALONE_TO_STAT: Dict[str, Tuple[str, str]] = {
    "sat": ("players_advanced_stats", "sat_pct"),
    "usat": ("players_advanced_stats", "usat_pct"),
    "gf": ("players_advanced_stats", "goals_pct"),
    "oz": ("players_advanced_stats", "oz_start_pct"),
    "so": ("players_season_stats", "shootout_pct"),
}

# Ключи adv:* для голевых лидеров по типу броска (меню /advanced)
SHOT_STANDALONE_TO_STAT: Dict[str, Tuple[str, str]] = {
    "wrist": ("players_shot_types", "goals_wrist"),
    "slap": ("players_shot_types", "goals_slap"),
    "snap": ("players_shot_types", "goals_snap"),
    "back": ("players_shot_types", "goals_backhand"),
    "tip": ("players_shot_types", "goals_tip_in"),
    "defl": ("players_shot_types", "goals_deflected"),
    "wrap": ("players_shot_types", "goals_wrap_around"),
}

# Все кнопки adv:* меню /advanced: кнопки, свободный текст (Задача 81) и его инструмент.
ADVANCED_METRICS: Dict[str, Tuple[str, str]] = {**ADV_STANDALONE_TO_STAT, **SHOT_STANDALONE_TO_STAT}

# Ряды кнопок меню: (подпись, ключ). Основные метрики /stats — ключ (таблица, колонка),
# первый ряд — бывший /leaders; расширенные — ключ ADVANCED_METRICS, ими кончается /stats
# и из них целиком состоит /advanced; командные — ключ TEAM_STAT_SPECS.
MAIN_STAT_ROWS: List[List[Tuple[str, Tuple[str, str]]]] = [
    [
        ("Очки", ("players_season_stats", "points")),
        ("Голы", ("players_season_stats", "goals")),
        ("Передачи", ("players_season_stats", "assists")),
    ],
    [
        ("+/−", ("players_season_stats", "plus_minus")),
        ("Хиты", ("players_season_stats", "hits")),
        ("Блоки", ("players_season_stats", "blocked")),
    ],
    [
        ("Штраф", ("players_season_stats", "pim")),
        ("Игровое время", ("players_season_stats", "time_on_ice_per_game")),
    ],
    [
        ("Победы", ("goalies_season_stats", "wins")),
        ("% отражённых", ("goalies_season_stats", "save_percentage")),
        ("Сухие", ("goalies_season_stats", "shutouts")),
    ],
]
ADVANCED_ROWS: List[List[Tuple[str, str]]] = [
    [("SAT %", "sat"), ("USAT %", "usat"), ("GF %", "gf")],
    [("OZ Start %", "oz"), ("Shootout %", "so")],
    [("Кистевой", "wrist"), ("Щелчок", "slap"), ("С полуприёма", "snap")],
    [("Бэкхенд", "back"), ("Направление", "tip"), ("Отклонение", "defl")],
    [("С за ворот", "wrap")],
]
TEAM_STAT_ROWS: List[List[Tuple[str, str]]] = [
    [("Голы", "goals"), ("Броски", "shots")],
    [("Большинство", "pp"), ("Меньшинство", "pk")],
    [("Вбрасывания", "fo")],
]
