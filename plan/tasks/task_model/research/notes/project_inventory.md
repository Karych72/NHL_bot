# Инвентаризация данных и модельной инфраструктуры NHL_bot (для прематч-модели против букмекера)

Аудит локального репозитория `/Users/petrkarol/Desktop/projects/NHL_bot` (master @ 78d3e78) на 2026-10-09.
Источники — файлы репозитория (пути относительно корня репо), кэш сырого API на диске и
read-only запросы к живой БД (`docker exec nhl_bot-db-1 psql -U postgres`, только SELECT).
Ссылка вида `[DB query 2026-10-09]` означает собственный SELECT к живой БД в день аудита.

## 1. Таблицы и колонки БД, полезные для моделирования

### Takeaway
В БД есть результат и тип развязки (OT/SO) каждой игры, финальный счёт (с +1 победителю буллитов),
командный бокс-скор, повратарная и поигроковая статистика за игру и список голов с флагами
пустых ворот/PP/SH. Чего нет: координат бросков, отдельного счёта основного времени, флага
стартового вратаря, времени начала матча, коэффициентов букмекеров.

### Cited Findings
**`games`** — `game_id, day (date), home_team_id, away_team_id, winner_id, is_overtime, is_shootouts, season, season_id`; колонок счёта в `games` нет — [data_tables/t.games.sql](../../../../../data_tables/t.games.sql)
- `winner_id` вычисляется загрузчиком из `homeScore`/`visitingScore` Stats REST API (`/stats/rest/en/game`, `gameType=2`, `gameStateId=7`) — только регулярный сезон — [pipeline/load_season_modern.py:834-840, 995-1000](../../../../../pipeline/load_season_modern.py)
- `is_overtime = periodType == "OT" OR period number > 3` по финальному `periodDescriptor` PBP → **истинно и для игр, решённых буллитами** (у буллитов `periodType: SO, number: 5`); `is_shootouts = gameOutcome.lastPeriodType == "SO"` — [pipeline/load_season_modern.py:1015-1021](../../../../../pipeline/load_season_modern.py)
- Проверка в БД: в 2024-25 `is_overtime` = 271 = 194 OT + 77 SO (совпадает с подсчётом `gameOutcome.lastPeriodType` по кэшу: REG 1041 / OT 194 / SO 77) — [DB query 2026-10-09]; кэш `NHL_bot-task-23/all_data/raw/20242025`
- Т.е. исход «основное время / OT / SO» восстанавливается: REG = `NOT is_overtime`; OT = `is_overtime AND NOT is_shootouts`; SO = `is_shootouts`. По 5 полным сезонам: 475 игр SO, 986 OT-без-SO — [DB query 2026-10-09]

**Счёт**: хранится в `game_team_stats.goals` (по строке на команду, `field` = 'home'/'away') — это финальный официальный счёт **включая +1 победителю серии буллитов**. Пример: игра 2024020022 закончилась 2:2 в основное+OT, в БД `goals` 2 / 3; во всех 475 SO-играх разница ровно 1 — [DB query 2026-10-09]; [pipeline/load_season_modern.py:1206-1246](../../../../../pipeline/load_season_modern.py)
- Счёт основного времени (60 мин) в явном виде не хранится, но есть `fst_period_goals, snd_period_goals, trd_period_goals` (голы только периодов 1–3) → reg-счёт = их сумма — [data_tables/t.game_team_stats.sql](../../../../../data_tables/t.game_team_stats.sql); [pipeline/load_season_modern.py:1046-1057](../../../../../pipeline/load_season_modern.py)

**`game_team_stats`** (по команде за игру): `goals, field, pim, shots (SOG из boxscore), power_play_percentage, power_play_goals, power_play_opportunities, face_off_win_percentage, blocked, takeaways, giveaways, hits, fst/snd/trd_period_goals` — [data_tables/t.game_team_stats.sql](../../../../../data_tables/t.game_team_stats.sql)
- Всё, кроме `goals`/`shots`, считается загрузчиком из событий PBP: PIM = сумма `duration` штрафов; PP-возможности = **число штрафов соперника** (не реальных розыгрышей большинства — поэтому бывают `power_play_percentage` > 100, клиппятся в билдере); `blocked` приписывается обороняющейся команде — [pipeline/load_season_modern.py:1110-1204](../../../../../pipeline/load_season_modern.py); [modeling/dataset_builder/team_game_facts.py](../../../../../modeling/dataset_builder/team_game_facts.py)
- Нет: попыток бросков (Corsi/Fenwick), промахов, xG, времени в большинстве.

**`game_goalie_stats`** (по вратарю за игру): `team_id, game_id, player_id, timeonice (mm:ss текст), assists, goals, pim, shots (= shotsAgainst), saves, power_play/short_handed/even saves и shots_against, decision (W=true / иначе false / NULL), save_percentage` и сплиты %, — [data_tables/t.game_goalie_stats.sql](../../../../../data_tables/t.game_goalie_stats.sql); [pipeline/load_season_modern.py:1300-1330](../../../../../pipeline/load_season_modern.py)
- **Флага стартера в БД нет**, хотя в кэшированном boxscore у вратаря есть поле `starter` (пример: `starter=False, toi 00:00` у запасного; `starter=True, toi 65:00, shotsAgainst 31`) — [кэш `NHL_bot-task-23/all_data/raw/20242025/2024020022.box.json.gz`]. Стартер косвенно выводим из максимального `timeonice`.
- Запасные вратари с 00:00 тоже записываются: 26 270 строк на 6 568 игр (~4 на игру) — [DB query 2026-10-09]
- Объявленного будущего стартера (для pre-game) нет нигде — [plan/archive/tasks/task_40_model_quality.md, «40d»](../../../../../plan/archive/tasks/task_40_model_quality.md)

**`game_player_stats`** (по полевому игроку за игру): `time_on_ice (mm:ss текст), goals, assists, shots, hits, PP/SH goals/assists, PIM, faceoffs, takeaways, giveaways, blocked, plus_minus` — ни один модуль её сейчас не читает — [data_tables/t.game_player_stats.sql](../../../../../data_tables/t.game_player_stats.sql). В boxscore доступно ещё `shifts`, но не сохраняется — [кэш box.json.gz, ключи skater]

**`all_goals`** (по голу): `goal_player_id, assists, empty_net, winner_goal, is_ppg, is_shg, team_id, game_id, period, time (mm:ss в периоде), goals_away, goals_home (текущий счёт), event_id` — [data_tables/t.all_goals.sql](../../../../../data_tables/t.all_goals.sql)
- `empty_net` = модификатор `empty-net` ИЛИ `situationCode` показывает пустые ворота соперника; `is_ppg/is_shg` выводятся из `situationCode` — [pipeline/load_season_modern.py:1058-1083](../../../../../pipeline/load_season_modern.py)
- **Ловушка:** голы серии буллитов (period=5) тоже лежат в `all_goals` как обычные строки — 1 065 из 41 583 строк; 2 395 голов в пустые ворота — [DB query 2026-10-09]. Подсчёт голов по `all_goals` без фильтра `period <= 4` завысит тоталы.
- Координат/типа броска в `all_goals` нет (они есть только в сыром PBP, см. п. 2).

**`scheduled_games`**: `game_id, day, home_team_id, away_team_id, season_id` — будущие игры регулярки, **окно только сегодня+завтра (UTC)**, таблица сезона полностью перезаписывается каждым прогоном — [data_tables/t.scheduled_games.sql](../../../../../data_tables/t.scheduled_games.sql); [pipeline/load_season_modern.py:42-45, 842-860](../../../../../pipeline/load_season_modern.py). На 2026-10-09 в ней 14 игр сезона 20262027 — [DB query 2026-10-09]

**`rosters`**: `player_id, season_id, name, position, jersey_number, currentage, nationality, captain, rookie, current_team_id` — сезонный, не дневной (нет истории переходов/травм) — [data_tables/t.rosters.sql](../../../../../data_tables/t.rosters.sql)

Прочие таблицы (не используются моделью): `teams, teams_stats, players_season_stats, goalies_season_stats, players_advanced_stats, players_shot_types, game_three_stars` — [data_tables/](../../../../../data_tables/). Сезонные агрегаты — «на конец/текущий момент сезона», не as-of, т.е. для pre-game фич опасны утечкой (вывод, см. Inferences).

**Коэффициентов букмекеров нет нигде**: grep `odds|bookmaker|moneyline` по `modeling/ pipeline/ telegram_bot/ data_tables/` находит только «log-odds» в калибровке — [modeling/calibrate.py:51](../../../../../modeling/calibrate.py)

### Inferences
- Для тотала «OT включён, буллиты = +1 гол победителю» (обычное букмекерское правило) нужный счёт уже лежит в `game_team_stats.goals`; для тотала/исхода «основное время» — сумма `*_period_goals`.
- Для Пуассон/Скеллам-модели голов основного времени и голов в OT лучше брать reg-счёт из периодов, а OT/SO-исход моделировать отдельно.
- Стартового вратаря в истории можно восстановить по max TOI или из кэша (`starter`), но для боевого прогноза источника объявленного стартера нет.
- Сезонные таблицы (`goalies_season_stats` и т.п.) не as-of; использовать их как фичи без пересчёта по дням — утечка.

### Gaps
- Не проверял, содержит ли Stats REST `homeScore` +1 за SO во всех сезонах — проверено только на примере и через инвариант «во всех 475 SO-играх |разница| = 1».
- Время начала матча (`startTimeUTC` есть в PBP) в БД не хранится — только `day`; влияние на фичи «дни отдыха» не оценивалось.

## 2. Play-by-play на уровне бросков: хранится ли и где

### Takeaway
В БД бросков нет, но загрузчик кэширует на диск сырые gzip-JSON (play-by-play, boxscore, landing) каждой сыгранной игры. Полный кэш на все 5 сезонов (6 560 игр × 3 файла, 132 МБ) лежит в соседнем worktree `NHL_bot-task-23`; в основном checkout — только 5 игр 2026-27. В PBP есть `xCoord/yCoord`, `shotType`, `situationCode`, `zoneCode`, `goalieInNetId`, `shootingPlayerId` для shot-on-goal / missed-shot / blocked-shot / goal.

### Cited Findings
- Кэш: `RAW_CACHE_DIR = <repo>/all_data/raw`, файл `all_data/raw/{season_id}/{game_id}.{pbp|box|landing}.json.gz`; кэшируется только финальный `gameState == "OFF"`, бессрочно — [pipeline/load_season_modern.py:26-49, 925-960](../../../../../pipeline/load_season_modern.py) (Задача 31)
- В docker-сервисе `sync` каталог `/app/all_data` смонтирован на именованный том `syncdata`, т.е. боевой кэш живёт в docker-томе, а не в рабочей копии — [docker-compose.yml:86-87, 125-126](../../../../../docker-compose.yml)
- Фактическое наличие на хосте (подсчёт файлов 2026-10-09):
  - `/Users/petrkarol/Desktop/projects/NHL_bot-task-23/all_data/raw/` — сезоны 20212022…20252026, по **1 312 pbp + 1 312 box + 1 312 landing** на сезон (= 6 560 игр, все игры в БД), 132 МБ
  - `NHL_bot/all_data/raw/20262027` — 5 pbp; `NHL_bot-task-62` — 41 pbp; `-task-31` — 56; `-task-56` — 18; `-task-34` — 7
- Структура PBP (проверено на `20242025/2024020001.pbp.json.gz`): верхние ключи `plays, rosterSpots, periodDescriptor, gameOutcome, startTimeUTC, homeTeam{score,sog}, venue, …`; ~349 событий; типы `hit, faceoff, shot-on-goal, stoppage, missed-shot, blocked-shot, giveaway, takeaway, penalty, goal, delayed-penalty, period-start/end, game-end`
- Пример события броска: `{"typeDescKey":"shot-on-goal","periodDescriptor":{...},"timeInPeriod":"00:08","situationCode":"1551","homeTeamDefendingSide":"right","details":{"xCoord":56,"yCoord":-39,"zoneCode":"O","shotType":"wrist","shootingPlayerId":...,"goalieInNetId":...,"eventOwnerTeamId":1,"awaySOG":1,"homeSOG":0}}`; `missed-shot` дополнительно `reason`; `blocked-shot` — `blockingPlayerId`; `goal` — `shotType`, `scoringPlayerId`, ассистенты, `goalieInNetId`, `homeScore/awayScore` — [кэш `NHL_bot-task-23/all_data/raw/20242025/2024020001.pbp.json.gz`]
- `situationCode` — 4 цифры: away goalie, away skaters, home skaters, home goalie (так интерпретирует загрузчик) — [pipeline/load_season_modern.py:1071-1083](../../../../../pipeline/load_season_modern.py)
- События буллитов в PBP — `periodType: "SO"`, `number: 5`, `situationCode` 1010/0101, в т.ч. `goal` с координатами — [кэш `20242025/2024020022.pbp.json.gz`]
- В boxscore у вратарей поле `starter`, у полевых — `toi`, `shifts`; `landing.summary` содержит `scoring, penalties, shootout, threeStars` — [кэш `20242025/2024020022.{box,landing}.json.gz`]

### Inferences
- Командный xG-подобный показатель (Corsi/Fenwick, расстояние/угол броска, shot type, 5v5 vs PP) можно посчитать офлайн из кэша без новых сетевых загрузок и без новых пакетов — но потребует нового кода парсинга и, вероятно, новой таблицы (решение человека, Global Constraints 2/5).
- Кэш в worktree `task-23` — не каноническое хранилище (worktree может быть удалён); для воспроизводимости его надо переносить/перекачивать. Память проекта предупреждает, что данные в БД уже однажды терялись.

### Gaps
- Не проверял полноту координат (доля событий без `xCoord`) по всем сезонам.
- Содержимое тома `syncdata` (боевой кэш 2026-27) не проверялось.

## 3. Текущий датасет-билдер (feature set v2)

### Takeaway
Датасет строится только из `games` + `game_team_stats`: скользящие средние 8 бокс-скор метрик в окнах 5/10/20, «последняя игра» снапшот, отдых/b2b, плюс as-of Elo. В манифесте 215 фич (не 212 — 212 было до Elo), 6 049 строк. Метки: `y_home_win` (с OT/SO) и `y_over_5_5` по финальному счёту (включая OT-голы и +1 за буллиты).

### Cited Findings
- Источники SQL: `games` JOIN `game_team_stats` (home/away goals как `*_goals_target`) — [modeling/dataset_builder/base.py:86-125](../../../../../modeling/dataset_builder/base.py); `data_snapshot_id: db:games+game_team_stats|…` — [metadata_train.json в `NHL_bot-task-40/artifacts/datasets/`]
- Метаданные прогона Задачи 40: `feature_set_version v2`, `rolling_windows [5,10,20]`, `min_prior_games 5`, `cold_start_policy_predict allow_with_flag`, `dataset_rows 6049`, `features_hash bb0c2b06…`, **215 фич в `feature_manifest`** — [NHL_bot-task-40/artifacts/datasets/metadata_train.json]; карточка Задачи 40 говорит «212 колонок» — это состояние до добавления `home_elo, away_elo, diff_elo` — [plan/archive/tasks/task_40_model_quality.md](../../../../../plan/archive/tasks/task_40_model_quality.md)
- Состав фич: `ROLLING_BASE_FIELDS = goals_for/against, shots_for/against, pim_for/against, power_play_percentage_for/against` × окна 5/10/20 (mean, `shift(1)`, `min_periods=1`); контекст `rest_days` (99 при отсутствии), `is_b2b` (rest ≤ 1), `games_last_7d`, `prior_games_count`; `goal_diff_roll_mean_5`, `pace_sum_roll_mean_5` — [modeling/dataset_builder/features.py:19-96](../../../../../modeling/dataset_builder/features.py)
- Плюс снапшот сырых значений **предыдущей** игры команды (`home_goals_for`, `home_hits_for`, `home_face_off_win_percentage_for`, … — все `game_team_stats` пары for/against) и `diff_*`/`sum_*` для каждой пары home/away — [manifest]; [modeling/dataset_builder/assemble.py](../../../../../modeling/dataset_builder/assemble.py)
- Окна и снапшоты группируются по `(team_id, season_id)` — сброс на границе сезона (Задача 30) — [docs/modeling_dataset_builder.md, «Anti-Leakage Contract»](../../../../../docs/modeling_dataset_builder.md)
- As-of: `hist_day < target_day` строго; `shift(1)`; игры того же дня маскируются (`intra_day_prev`); валидатор падает при нарушении — [docs/modeling_dataset_builder.md](../../../../../docs/modeling_dataset_builder.md); [modeling/dataset_builder/validate.py](../../../../../modeling/dataset_builder/validate.py)
- Cold-start: train — drop строк с `prior_games_count < min_prior_games=5` (511 из 6 560 отброшено); predict — `allow_with_flag` (флаг `low_history_confidence`), но NaN у первых игр сезона роняют валидатор — [docs/modeling_dataset_builder.md, assemble.py](../../../../../docs/modeling_dataset_builder.md); [CLAUDE.md, GC 8](../../../../../CLAUDE.md)
- Метки: `y_home_win = winner_id == home_team_id` (OT/SO победа = победа); `y_over_5_5 = (home_goals + away_goals) > 5.5` по `game_team_stats.goals` → **включает голы OT и +1 за победу в буллитах** — [modeling/dataset_builder/assemble.py:95-97](../../../../../modeling/dataset_builder/assemble.py); [DB query 2026-10-09: 779 игр с OT/SO и тоталом > 5.5]
- Elo: `ELO_START=1500`, `K=8`, `HFA=35`, MOV-множитель `ln(|gd|+1)·2.2/(0.001·winner_edge+2.2)`, OT/SO-победа = полная победа, регрессия на 1/3 к среднему при смене сезона; рейтинги замораживаются на день; predict берёт рейтинг после последней сыгранной игры без межсезонной регрессии; подобраны грид-поиском по log-loss на играх 2022-10-01…2025-11-19 (топ-8 настроек в пределах 0.0004) — [docs/modeling_dataset_builder.md, «Elo team-strength feature»](../../../../../docs/modeling_dataset_builder.md); [modeling/dataset_builder/features.py:191-330](../../../../../modeling/dataset_builder/features.py)
- Сигнал Elo (raw log-loss vs константа сезона): 2022-23 0.6612 vs 0.6924; 2023-24 0.6611 vs 0.6900; 2024-25 0.6670 vs 0.6870; 2025-26 0.6897 vs 0.6929 (сезон аномально паритетный, home-win 0.522 vs 0.541) — [docs/modeling_dataset_builder.md](../../../../../docs/modeling_dataset_builder.md)
- Нет фич: вратарь, составы/травмы, перелёты/часовые пояса, xG/броски, коэффициенты рынка — [plan/archive/tasks/task_40_model_quality.md, 40d «не начата»](../../../../../plan/archive/tasks/task_40_model_quality.md)

### Inferences
- Фичи `home_is_home`/`diff_is_home` и снапшоты «прошлой игры» почти наверняка шум; 215 колонок на ~6k строк — избыточно (40a: «сигнала в 212 rolling-колонках нет»).
- Т.к. `y_over_5_5` считается по финальному счёту с SO-голом, линия 5.5 соответствует букмекерскому тоталу «с OT/SO», что правильно для сравнения с рынком.

### Gaps
- Почему в `metadata_train.json` `data_snapshot_id` содержит `seasons=all` (документ требует явный `--season-ids`) — прогон Задачи 40 собран без явного списка; на результат не влияет, но воспроизводимость слабее.

## 4. Обучение, валидация, гейт, результаты Задачи 40

### Takeaway
Две модели (логрегрессия с сеткой C и LightGBM с 8-точечной сеткой и монотонными ограничениями), walk-forward по месяцам (5 окон, inner_val 300, calibration 300), Platt-калибровка, holdout = последние 15% уникальных дней (994 игры). `home_win` еле бьёт константу (Δ 0.002–0.003 log-loss, ДИ включают 0); `over_5_5` хуже константы обеими моделями.

### Cited Findings
- Конфиг: `random_seed 42`; `split.method: month, n_test_windows 5, inner_val_games 300, calibration_games 300, holdout.fraction 0.15`; logreg `C ∈ {0.01,0.1,1,10}`; lgbm `num_leaves {7,15} × min_data_in_leaf {100,200} × learning_rate {0.03,0.05}`, остальное фикс.; monotone для `home_win` по `diff_goals_*`, `diff_elo:+1`; `calibration.method: platt, min_samples 300`; `ece_bins 10`, bootstrap 1000 block-by-day — [configs/modeling_default.yaml](../../../../../configs/modeling_default.yaml)
- Walk-forward: holdout вырезается из хвоста до построения окон; expanding train; блоки inner_val/calibration/test последовательны; без embargo — [modeling/splits.py:1-24, 161-248](../../../../../modeling/splits.py)
- Финальный lgbm переобучается на `train_full` фиксированным `best_iteration` без early stopping (раньше утечка давала 500 раундов и LL 0.7646) — [docs/modeling_training.md §4](../../../../../docs/modeling_training.md)
- Platt = нерегуляризованная LogisticRegression на `logit(clip(p))`; isotonic на 300 играх давал 0/1 и LL 0.887 — [docs/modeling_training.md §7](../../../../../docs/modeling_training.md)
- Метрики: log-loss, Brier, ECE до/после калибровки, block-bootstrap 95% ДИ, trivial baseline, reliability PNG, разбивка по командам — [docs/modeling_training.md §6](../../../../../docs/modeling_training.md); [modeling/metrics.py](../../../../../modeling/metrics.py)
- Гейт: для задачи выбирается семейство с минимальным калиброванным holdout LL, оно должно **строго** побить `trivial_base_rate`; вердикт наследуют все пары задачи; `latest` пишется только при `status ok`; общий статус прогона ok только если все задачи прошли — [docs/modeling_training.md §8](../../../../../docs/modeling_training.md); [modeling/acceptance.py](../../../../../modeling/acceptance.py)
- Результаты Задачи 40 (holdout 994 игры, 2025-11-20…2026-04-16, run `…_bb0c2b06_20260928T183528Z`) — [docs/modeling_training.md §8](../../../../../docs/modeling_training.md):

| task | model | raw LL | cal LL | trivial LL | Δ (trivial − model) | 95% ДИ Δ(model − trivial) | acceptance |
|---|---|---|---|---|---|---|---|
| home_win | logreg | 0.703489 | 0.691975 | 0.693761 | +0.001786 | [−0.004412, +0.000797] | ok |
| home_win | lgbm | 0.691250 | 0.690900 | 0.693761 | +0.002861 | [−0.005666, +0.000003] | ok |
| over_5_5 | logreg | 0.697096 | 0.682794 | 0.682040 | −0.000753 | [−0.001474, +0.003137] | failed_baseline_check |
| over_5_5 | lgbm | 0.694964 | 0.683514 | 0.682040 | −0.001474 | [−0.001636, +0.004535] | failed_baseline_check |

- Вывод документа: запас в пределах шума; 994-игровые block-bootstrap ДИ шириной ±0.003–0.01, «значимый» проход на таком holdout недостижим; для `over_5_5` сигнала нет ни в rolling-фичах, ни в Elo-подобном рейтинге голов (спайк 40a) — [docs/modeling_training.md §8](../../../../../docs/modeling_training.md)
- Ориентир из карточки: приличные публичные модели `home_win` ~0.66–0.68 LL против ~0.69 базовой — [plan/archive/tasks/task_40_model_quality.md, «Подводные камни»](../../../../../plan/archive/tasks/task_40_model_quality.md) (утверждение карточки, не измерение)
- Holdout содержит олимпийскую паузу (нет игр 2026-02-05…02-25) — [plan/archive/tasks/task_40_model_quality.md](../../../../../plan/archive/tasks/task_40_model_quality.md)
- Еженедельный retrain (пн 12:00 UTC) только `home_win`, `--no-promote`; `latest` двигает человек; время прогона ~45 с — [docs/modeling_training.md §9](../../../../../docs/modeling_training.md)

### Inferences
- Гейт сравнивает только с константой, не с рынком; для задачи «против букмекера» нужна иная метрика (LL против де-виженных вероятностей рынка, CLV, ROI) — в коде её нет.
- Raw Elo в сезонах 2022–25 давал LL ~0.661–0.667, а финальная модель на holdout 0.691 — основная проблема в аномальном сезоне 2025-26 и шуме, а не в архитектуре.

### Gaps
- Не перепроверял числа `metrics.json` (артефакты в worktree task-40, git-ignored).

## 5. Объём данных в БД, включая сезон 2026/27

### Takeaway
5 полных регулярных сезонов по 1 312 игр (2021-22…2025-26, всего 6 560) плюс 8 сыгранных игр 2026-27 и 14 игр в `scheduled_games`.

### Cited Findings
- По сезонам (games / min day / max day / is_overtime / is_shootouts): 20212022 1312 (2021-10-12…2022-05-01, 288/102); 20222023 1312 (2022-10-07…2023-04-14, 302/95); 20232024 1312 (…2024-04-18, 272/82); 20242025 1312 (2024-10-04…2025-04-17, 271/77); 20252026 1312 (2025-10-07…2026-04-16, 326/119); 20262027 **8** (2026-09-29…2026-10-07, 2/0) — [DB query 2026-10-09]
- Совпадает с CLAUDE.md GC 8: «5 полных сезонов, 6560 игр, плюс начатый 2026/27 — 8 игр (2026-10-08, Задача 62)» — [CLAUDE.md](../../../../../CLAUDE.md)
- `game_predictions` на 2026-10-09 пуста (0 строк) — [DB query 2026-10-09]
- Только регулярный сезон (`gameType=2`), плей-офф не грузится — [pipeline/load_season_modern.py:836](../../../../../pipeline/load_season_modern.py)

### Inferences
- ~6.5k игр — достаточно для Пуассон/Скеллам с командными атакой/обороной и для Elo, мало для 200+ фич.

### Gaps
- Ранние даты 2026-27 (с 2026-09-29) не проверялись на тип игры (возможно, зарубежные матчи открытия) — не влияет на инвентарь.

## 6. Ограничения проекта (CLAUDE.md) и зависимости

### Takeaway
Любой новый пакет (например, statsmodels/PyMC) требует явного разрешения человека; разрешён только стек numpy/pandas/scipy (транзитивно)/scikit-learn/lightgbm/matplotlib/pyyaml/joblib/pydantic.

### Cited Findings
- GC1 YAGNI (абстракция с третьего повторения); GC2 расширять существующие модули; GC3 без обратной совместимости — замена удаляет старое; GC4 падать громко; GC5 новый пакет — только с разрешения, ML → `requirements-modeling.txt`; GC6 >300 строк чистого прироста — Important; GC7 docstring + обновление `docs/architecture.md`; GC8 масштаб под фактический объём данных — [CLAUDE.md](../../../../../CLAUDE.md)
- `requirements-modeling.in`: `numpy==2.2.6, pandas==2.2.3, scikit-learn==1.6.1, lightgbm==4.6.0, matplotlib==3.10.1, pyyaml==6.0.2, joblib==1.4.2, pydantic==2.10.6`; в lock транзитивно есть `scipy==1.17.1` — [requirements-modeling.in](../../../../../requirements-modeling.in); [requirements-modeling.txt](../../../../../requirements-modeling.txt)
- Modeling-стек не входит в образ бота; отдельная стадия Dockerfile `modeling` — [docs/architecture.md:312-322](../../../../../docs/architecture.md)
- Ревью любой задачи — Opus; одна задача плана = одна сессия — [CLAUDE.md](../../../../../CLAUDE.md)

### Inferences
- Пуассон/Скеллам/Dixon-Coles реализуемы на `scipy.stats`/`scipy.optimize` и `sklearn.linear_model.PoissonRegressor` без новых зависимостей; statsmodels, PyMC, xgboost — потребуют разрешения.

### Gaps
- Нет.

## 7. Куда встраивать Пуассон-модель голов

### Takeaway
Задачи захардкожены как бинарные: `TASK_LABEL_MAP = {home_win, over_5_5}`, CLI `choices`, `TASK_NAMES`, монотонные ограничения и гейт по log-loss против константы. Голевая модель естественно встаёт как новый способ получать P(home_win) и P(total > line) для существующих задач, а публикация идёт в `game_predictions` (одна вероятность на игру и задачу).

### Cited Findings
- `modeling/train_common.py:22-27`: `TASK_LABEL_MAP = {"home_win": "y_home_win", "over_5_5": "y_over_5_5"}`; `SUPPORTED_TASKS` — [modeling/train_common.py](../../../../../modeling/train_common.py)
- `modeling/train_runner.py:58`: `TASK_NAMES = ("home_win", "over_5_5")`; семейства моделей — logreg/lgbm через `predict_raw_proba` dispatch — [modeling/train_runner.py](../../../../../modeling/train_runner.py); [docs/modeling_training.md §7](../../../../../docs/modeling_training.md)
- CLI подкоманды `build-dataset | train | promote | predict | publish-predictions`; `--task` choices `home_win, over_5_5`; `--model` choices `logreg, lgbm` — [modeling/cli.py:29-158](../../../../../modeling/cli.py)
- Train-путь не ходит в БД (только `dataset_train.csv` + `metadata_train.json` через `train_input.load_training_table_split`); метки — `LABEL_COLUMNS` в `schema.py`; таргет-голы `home_goals_target/away_goals_target` выбрасываются из датасета после построения меток (защита от утечки, `diff_goals_target` запрещён валидатором) — [modeling/dataset_builder/assemble.py:122-124](../../../../../modeling/dataset_builder/assemble.py); [modeling/dataset_builder/validate.py:25](../../../../../modeling/dataset_builder/validate.py)
- Predict: `predict_runner.run_predict` → CSV `game_id, day, season_id, home_team_id, away_team_id, probability` — [docs/modeling_training.md §7](../../../../../docs/modeling_training.md)
- Публикация: `publish-predictions` удаляет строки задачи и вставляет новые в одной транзакции; гейт — `status` в `latest/metadata.json` — [modeling/publish_predictions.py](../../../../../modeling/publish_predictions.py)
- Схема `game_predictions(game_id, task text, model text, run_id text, probability double CHECK 0..1, computed_at, PK(game_id, task))` — нет колонок для линии тотала, ожидаемых голов λ, коэффициентов — [data_tables/t.game_predictions.sql](../../../../../data_tables/t.game_predictions.sql)
- Бот читает только `task = 'home_win'` — [telegram_bot/bot_messages.py:799-804](../../../../../telegram_bot/bot_messages.py)

### Inferences
- Минимальный путь: добавить в датасет таргеты счёта (сейчас вырезаются) как отдельные label-колонки и семейство `poisson` в `--model` для существующих задач — тогда гейт, отчёты, `latest`, predict и publish переиспользуются без изменения схемы БД. Таргет «голы» позволит выдавать P(total > любая линия), но под текущую схему публикуется лишь одна вероятность на (game, task) — для нескольких линий или λ потребуется миграция `game_predictions` (новая колонка/таск).
- Нужно решить, моделировать ли reg-счёт (периоды 1–3) + отдельную модель OT/SO, или финальный счёт; SO-гол (+1) в Пуассон не вписывается.

### Gaps
- Не просматривал `train_runner.py` построчно (как именно фолды вызывают `train_*_for_task`) — оценка трудоёмкости встраивания приблизительная.
