"""Заголовки для пагинируемых таблиц лидеров: ключ (таблица, колонка) → название в чате."""

from typing import Dict, Tuple

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

# Команды — только колонка в teams_stats
TEAM_STAT_TITLES: Dict[str, str] = {
    "procent_points": "Статистика процента набранных очков",
    "power_play_percentage": "Статистика большинства",
    "penalty_kill_percentage": "Статистика меньшинства",
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
