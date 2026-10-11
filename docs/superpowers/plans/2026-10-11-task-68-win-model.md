# Задача 68: модель вероятности победы — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `home_win` — логрег на 4 признаках (`diff_elo`, `home_is_b2b`, `away_is_b2b`,
`diff_shot_share_ewm`), без Platt, без прогревочного сезона в обучении; гейт — log-loss ниже Elo
и ECE не выше Elo; ветка `lgbm` для `home_win` удалена; retrain снова включён.

**Architecture:** вариант B спеки — модель встраивается в существующий конвейер
`dataset_builder` → `train_runner` → `predict` → `publish` → `retrain`. Новая колонка
считается межсезонным проходом по истории сыгранных игр рядом с Elo; `train_runner` получает для
`home_win` свой список признаков, своё семейство (`logreg`) и свою схему обучения (без прогрева,
без калибратора).

**Tech Stack:** Python 3, pandas, numpy, scikit-learn, pydantic, pytest; PostgreSQL только в
сборщике датасета.

**Spec:** `docs/superpowers/specs/2026-10-11-task-68-win-model-design.md` — читать вместе с планом.

**Рабочая копия:** `/Users/petrkarol/Desktop/projects/NHL_bot-task-68`, ветка `task-68-win-model`.
Все команды — из неё. Файлы менять инструментами Edit/Write, не через Bash (правки из Bash
теряются после `make ci-local`).

## Global Constraints

Из `CLAUDE.md` (обязательны для каждой задачи):
- GC1 YAGNI: никаких конфиг-флагов «на будущее»; константы признака — модульные, не конфиг.
- GC2: новых модулей не заводить — правим `features.py`, `base.py`, `assemble.py`, `config.py`,
  `train_runner.py`, `acceptance.py`, `metrics.py`, `predict_runner.py`, `cli.py`.
- GC3: старое удаляется (ветка `lgbm` для `home_win`), без совместимости со старыми артефактами.
- GC4: падать громко — пропуск бросков в сыгранной игре `ValueError`, неизвестный признак —
  `ConfigError`, пустой train после исключения прогрева — `ConfigError`.
- GC5: новых зависимостей нет.
- GC6: чистый прирост ≤ 300 строк на всю ветку (оценка ~250); превышение — сообщить.
- GC7: docstring у публичных функций (что / зачем / аргументы); `docs/` — в той же задаче.
- Перед каждым коммитом: `make ci-local` (ruff + mypy + compileall + pytest) — зелёный.
- Коммит-сообщения на русском, оканчиваются строкой
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` (или своей моделью).
- Константы признака: полураспад `20` игр, сжатие на смене сезона `0.5`, старт и центр `0.5`.
- Признаки `home_win` ровно: `[diff_elo, home_is_b2b, away_is_b2b, diff_shot_share_ewm]`.
- Рабочая БД — `postgres` (`PG_DATABASE` в `.env`), не `nhl_bot_predict_demo`; роль `postgres`.

## Review Focus

1. **Игры одного дня.** Две игры команды в один день не бывает, но игры разных команд в один
   день — постоянно: значение признака для игры дня D не должно видеть ни одной игры дня D.
   Тест в Задаче 1 (`test_same_day_games_do_not_see_each_other`).
2. **Predict для команды без истории / после межсезонья.** Будущая игра получает финальное
   состояние команды; если в истории последний сезон старше сезона цели — сжатие к 0.5 должно
   примениться (иначе в октябре прогноз несёт прошлогоднюю силу без сжатия). Тест в Задаче 1
   (`test_predict_row_after_season_change_is_shrunk`).
3. **`--task both --model lgbm`.** `over_5_5` обучается lgbm, `home_win` — ничего не обучает и
   не падает молча: пара `(home_win, lgbm)` просто не существует, а `--task home_win --model lgbm` —
   `ConfigError`. Тест в Задаче 3.
4. **Старый артефакт без `model_features`.** `predict` на артефакте до этой задачи падает с
   понятным `KeyError`/`ValueError`, а не подаёт в модель 215 колонок. Тест в Задаче 3.
5. **Гейт при равном ECE.** `model.ece == elo.ece` — проходит (не хуже), `model.ece > elo.ece` —
   отказ с причиной в `summary.md`. Тест в Задаче 4.

---

### Task 1: признак `shot_share_ewm` в сборщике датасета

**Files:**
- Modify: `modeling/dataset_builder/features.py` (новая функция `compute_pregame_shot_share`,
  обобщение аттача или отдельный `attach_pregame_shot_share`)
- Modify: `modeling/dataset_builder/base.py:95-125` (броски в `with_outcome`), `:386-388` (вызов)
- Modify: `modeling/dataset_builder/assemble.py:31` (без `sum_shot_share_ewm`)
- Test: `tests/test_modeling_dataset_build.py`
- Docs: `docs/modeling_dataset_builder.md` (раздел рядом с «Elo team-strength feature»)

**Interfaces:**
- Produces: `compute_pregame_shot_share(played_games: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, tuple[float, int]]]`
  — `per_game` с колонками `game_id`, `home_shot_share_ewm`, `away_shot_share_ewm`;
  финальное состояние `{team_id: (share, last_season_id)}`.
- Produces: колонки датасета `home_shot_share_ewm`, `away_shot_share_ewm`, `diff_shot_share_ewm`
  (float64), без `sum_shot_share_ewm`.
- Consumes: `elo_history` из `load_target_games(..., with_outcome=True)` + новые колонки
  `home_shots`, `away_shots`.

- [ ] **Step 1: тесты (падают)** — в `tests/test_modeling_dataset_build.py` новый класс:

```python
class TestShotShareEwm(unittest.TestCase):
    """Задача 68: межсезонная доля бросков с затуханием, as-of по дням."""

    @staticmethod
    def _played(rows):
        return pd.DataFrame(
            rows,
            columns=["game_id", "day", "season_id", "home_team_id", "away_team_id", "home_shots", "away_shots"],
        )

    def test_first_game_is_neutral_and_update_uses_alpha(self):
        from modeling.dataset_builder.features import SHOT_SHARE_ALPHA, compute_pregame_shot_share

        played = self._played([
            (1, "2025-10-01", 20252026, 10, 20, 30.0, 10.0),
            (2, "2025-10-03", 20252026, 10, 20, 20.0, 20.0),
        ])
        per_game, final = compute_pregame_shot_share(played)
        first = per_game.set_index("game_id").loc[1]
        self.assertEqual((first.home_shot_share_ewm, first.away_shot_share_ewm), (0.5, 0.5))
        second = per_game.set_index("game_id").loc[2]
        self.assertAlmostEqual(second.home_shot_share_ewm, 0.5 + SHOT_SHARE_ALPHA * (0.75 - 0.5))
        self.assertAlmostEqual(second.away_shot_share_ewm, 0.5 + SHOT_SHARE_ALPHA * (0.25 - 0.5))
        self.assertEqual(final[10][1], 20252026)

    def test_same_day_games_do_not_see_each_other(self):
        from modeling.dataset_builder.features import compute_pregame_shot_share

        played = self._played([
            (1, "2025-10-01", 20252026, 10, 20, 40.0, 10.0),
            (2, "2025-10-01", 20252026, 30, 10, 10.0, 40.0),  # team 10 again, same day
        ])
        per_game, _ = compute_pregame_shot_share(played)
        self.assertEqual(per_game.set_index("game_id").loc[2, "away_shot_share_ewm"], 0.5)

    def test_season_change_shrinks_toward_half(self):
        from modeling.dataset_builder.features import SHOT_SHARE_ALPHA, SHOT_SHARE_SEASON_KEEP, compute_pregame_shot_share

        played = self._played([
            (1, "2025-04-01", 20242025, 10, 20, 30.0, 10.0),
            (2, "2025-10-01", 20252026, 10, 20, 20.0, 20.0),
        ])
        per_game, _ = compute_pregame_shot_share(played)
        after_first = 0.5 + SHOT_SHARE_ALPHA * 0.25
        expected = 0.5 + SHOT_SHARE_SEASON_KEEP * (after_first - 0.5)
        self.assertAlmostEqual(per_game.set_index("game_id").loc[2, "home_shot_share_ewm"], expected)

    def test_missing_shots_in_played_game_raises(self):
        from modeling.dataset_builder.features import compute_pregame_shot_share

        played = self._played([(1, "2025-10-01", 20252026, 10, 20, None, 10.0)])
        with self.assertRaises(ValueError):
            compute_pregame_shot_share(played)

    def test_predict_row_after_season_change_is_shrunk(self):
        from modeling.dataset_builder.features import (
            SHOT_SHARE_ALPHA, SHOT_SHARE_SEASON_KEEP, attach_pregame_shot_share, compute_pregame_shot_share,
        )

        played = self._played([(1, "2025-04-01", 20242025, 10, 20, 30.0, 10.0)])
        per_game, final = compute_pregame_shot_share(played)
        targets = pd.DataFrame({
            "game_id": [99], "day": [pd.Timestamp("2025-10-08")], "season_id": [20252026],
            "home_team_id": [10], "away_team_id": [77],  # 77: no history
        })
        out = attach_pregame_shot_share(targets, per_game, final)
        after_first = 0.5 + SHOT_SHARE_ALPHA * 0.25
        self.assertAlmostEqual(out.loc[0, "home_shot_share_ewm"], 0.5 + SHOT_SHARE_SEASON_KEEP * (after_first - 0.5))
        self.assertEqual(out.loc[0, "away_shot_share_ewm"], 0.5)

    def test_assemble_builds_diff_but_no_sum(self):
        snapshots = pd.DataFrame([{
            "game_id": 1, "day": "2026-01-01", "season_id": 20252026,
            "home_team_id": 10, "away_team_id": 20, "winner_id": 10,
            "home_goals_target": 4, "away_goals_target": 2,
            "home_prior_games_count": 8, "away_prior_games_count": 8,
            "home_shot_share_ewm": 0.55, "away_shot_share_ewm": 0.48,
        }])
        data, _ = assemble_dataset("train", snapshots, min_prior_games=0, cold_start_policy_predict="allow_with_flag")
        self.assertAlmostEqual(data.loc[0, "diff_shot_share_ewm"], 0.07)
        self.assertNotIn("sum_shot_share_ewm", data.columns)
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_modeling_dataset_build.py -k ShotShare -v` — FAIL (ImportError).

- [ ] **Step 3: реализация в `features.py`** (после `attach_pregame_elo`):

```python
# Задача 68: shot share of a team's games, smoothed over its games across seasons.
SHOT_SHARE_START = 0.5
SHOT_SHARE_HALFLIFE_GAMES = 20
SHOT_SHARE_ALPHA = 1.0 - 0.5 ** (1.0 / SHOT_SHARE_HALFLIFE_GAMES)
SHOT_SHARE_SEASON_KEEP = 0.5


def _shrink_share(share: float) -> float:
    return SHOT_SHARE_START + SHOT_SHARE_SEASON_KEEP * (share - SHOT_SHARE_START)


def compute_pregame_shot_share(played_games: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, tuple[float, int]]]:
    """Pre-game exponentially smoothed shot share of both teams, plus each team's final state.

    The ``home_win`` model's shot-share feature (Задача 68). A team's share of the shots in a
    game, ``shots_for / (shots_for + shots_against)``, updates ``s += α·(x − s)`` (half-life
    ``SHOT_SHARE_HALFLIFE_GAMES``), starting at 0.5; at a team's first game of a new season it
    is shrunk toward 0.5 by ``SHOT_SHARE_SEASON_KEEP``. Like Elo it runs over the whole played
    history, not per season, and a day's games all read the state frozen at the start of that
    day.

    Args:
        played_games: one row per played game with ``game_id``, ``day``, ``season_id``,
            ``home_team_id``, ``away_team_id``, ``home_shots``, ``away_shots``.

    Returns:
        ``(per_game, final_state)``: ``game_id``, ``home_shot_share_ewm``,
        ``away_shot_share_ewm`` per game; ``{team_id: (share, season_id of its last game)}``.

    Raises:
        ValueError: a played game lacks shots, or both sides have zero shots.
    """
```

Тело — цикл по играм, отсортированным по `(day, game_id)`: на смене дня применить отложенные
обновления; для каждой стороны взять состояние `(share, season)`, при `season != season_id игры`
сначала `_shrink_share`; записать значение; обновления дня копить в списке. Проверка пропусков
и нулевой суммы — до цикла (`ValueError` с `game_id` первой плохой игры).

`attach_pregame_shot_share(target_games, per_game, final_state)` — как `attach_pregame_elo`:
merge по `game_id`; строки без совпадения берут `final_state[team]`, сжатое, если
`season_id` цели > сезона в состоянии; команда без состояния — 0.5. Docstring обязателен.
Общий аттач с Elo не выносить (2-е повторение, GC1).

- [ ] **Step 4: `base.py`** — в `with_outcome` добавить
  `hs.shots::double precision AS home_shots, aws.shots::double precision AS away_shots`; в
  docstring `load_target_games` упомянуть броски для доли бросков. В `build_dataset` после
  `attach_pregame_elo`:

```python
    shot_per_game, shot_final = compute_pregame_shot_share(elo_history)
    snapshots = attach_pregame_shot_share(snapshots, shot_per_game, shot_final)
```

  Переименовать `elo_history` не требуется; комментарий над загрузкой дополнить: «и доля бросков».
  `_GAMES_TRAIN_COLUMNS` не трогать (броски в `games_train.csv` не нужны).

- [ ] **Step 5: `assemble.py:31`** — `if feature in ("elo", "shot_share_ewm"):` + строка комментария:
  сумма двух долей несёт не больше сигнала, чем разность.

- [ ] **Step 6:** `.venv/bin/python -m pytest tests/test_modeling_dataset_build.py -v` — PASS;
  существующие тесты сборщика, которые подают `elo_history` без бросков, поправить фикстурой
  (добавить `home_shots`/`away_shots`), а не ослаблением проверки.

- [ ] **Step 7: docs** — `docs/modeling_dataset_builder.md`: новый абзац «Shot-share feature
  (Задача 68)»: формула, константы, межсезонность и заморозка дня, predict-откат на финальное
  состояние со сжатием, `ValueError` на пропуске, отсутствие `sum_`. Указать, что колонки меняют
  `features_hash` → нужна пересборка train и predict.

- [ ] **Step 8:** `make ci-local` — зелёный; коммит
  `Задача 68: признак доли бросков с затуханием в датасете`.

---

### Task 2: конфиг — список признаков задачи, без `lgbm.monotone.home_win`

**Files:**
- Modify: `modeling/config.py:58-70` (`TaskToggle.features`), `:180-205` (`LgbmMonotone` — только `over_5_5`)
- Modify: `configs/modeling_default.yaml`, `configs/modeling_smoke.yaml`
- Modify: `tests/_modeling_fixtures.py` (4 колонки в синтетическом датасете и манифесте)
- Test: `tests/test_modeling_config.py`

**Interfaces:**
- Produces: `TaskToggle.features: list[str] | None` (None — все колонки датасета).
- Produces: в синтетическом датасете колонки `diff_elo`, `home_is_b2b`, `away_is_b2b`,
  `diff_shot_share_ewm`; `diff_elo` несёт сигнал `y_home_win`.

- [ ] **Step 1: тесты (падают)** в `tests/test_modeling_config.py`:
  - `features` для `home_win` в обоих конфигах равен ровно списку из Global Constraints;
  - у `over_5_5` `features is None`;
  - `features: []` — ошибка валидации (пустой список запрещён, `min_length=1`);
  - `models.lgbm.monotone.home_win` в YAML — ошибка (`extra="forbid"`);
  - существующий `TestLgbmMonotoneNamesMatchDataset` — оставить только для `over_5_5`, плюс новый
    тест: каждое имя `tasks.home_win.features` есть среди колонок, которые производит реальный
    `dataset_builder` (тем же способом, каким тест проверяет monotone-ключи).

- [ ] **Step 2:** запустить — FAIL.

- [ ] **Step 3: реализация.**

```python
class TaskToggle(_ForbidExtraModel):
    enabled: bool
    # Задача 68: the task's own feature columns; unset = every feature column of the dataset.
    features: Optional[List[str]] = Field(default=None, min_length=1)
```

  `LgbmMonotone`: удалить поле `home_win` и его валидатор. YAML обоих конфигов:

```yaml
tasks:
  home_win:
    enabled: true
    # Задача 68: logreg on four features (docs/modeling_training.md, «Результаты Задачи 68»).
    features: [diff_elo, home_is_b2b, away_is_b2b, diff_shot_share_ewm]
```

  и удалить блок `monotone.home_win` с его комментарием.

- [ ] **Step 4: фикстура** `write_synthetic_train_dataset`: добавить в строку
  `"diff_elo": 80.0 * (2 * home_win - 1) + float(rng.normal(0, 100))`,
  `"home_is_b2b": int(rng.integers(0, 2))`, `"away_is_b2b": int(rng.integers(0, 2))`,
  `"diff_shot_share_ewm": float(rng.normal(0, 0.03))`; те же имена (с dtype и позициями) — в
  `minimal_feature_manifest()` в порядке колонок CSV. Прогнать весь `tests/` — упавшие из-за
  изменения манифеста тесты чинить по месту.

- [ ] **Step 5:** `make ci-local` — зелёный; коммит
  `Задача 68: признаки home_win в конфиге, monotone home_win удалён`.

---

### Task 3: `train_runner`/`predict`/CLI — `home_win` только logreg, свои признаки, без прогрева и Platt

**Files:**
- Modify: `modeling/train_runner.py` (`TASK_MODEL_FAMILIES`, `resolve_models`, цикл сезонов,
  `_fit_final_raw_model`, метаданные)
- Modify: `modeling/predict_runner.py` (подача `model_features`)
- Modify: `modeling/cli.py` (`--model` для `home_win`), `Makefile:82-83` (`MODEL ?= logreg`)
- Test: `tests/test_modeling_cli_smoke.py`, `tests/test_modeling_predict_runner.py`,
  `tests/test_modeling_acceptance.py`, `tests/test_modeling_artifacts.py`,
  `tests/test_modeling_publish_predictions.py`, `tests/test_modeling_metrics.py` (убрать `home_win`+`lgbm`)

**Interfaces:**
- Consumes: `TaskToggle.features` (Task 2).
- Produces: `TASK_MODEL_FAMILIES: dict[str, tuple[str, ...]] = {"home_win": ("logreg",), "over_5_5": ("logreg", "lgbm")}`;
  `resolve_models(model_flag: str, task: str) -> list[str]` (`ConfigError`, если явное семейство
  задаче не положено; `"both"` → `TASK_MODEL_FAMILIES[task]`).
- Produces: `metadata.json` (сезонный и `final/`) несёт `"model_features": list[str]`.

Схема `home_win` (спека §2), реализовать одной функцией, используемой и в цикле сезонов, и в
финальной модели:

```python
def _fit_home_win(
    X: pd.DataFrame,
    y_all: np.ndarray,
    keys: pd.DataFrame,
    *,
    hp_train_idx: np.ndarray,
    inner_val_idx: np.ndarray,
    fit_idx: np.ndarray,
    config: ResolvedConfig,
    log_loss_fn: Any,
) -> FitResult:
    """``home_win`` model (Задача 68): C on ``inner_val``, then refit on every ``fit_idx`` row.

    Rows of the first season of the dataset (Elo warm-up: every team starts at 1500) are dropped
    from ``hp_train_idx`` and ``fit_idx``. There is no calibrator: the logistic fit on the
    training seasons is itself the fitted curve, and Platt on 300 games was measured to hurt.
    """
```

  - прогрев: `warmup = keys["season_id"].min()`; `keep = keys["season_id"].to_numpy()[idx] != warmup`;
    пустой `hp_train_idx` после фильтра — `ConfigError` с числами;
  - C — `select_C_by_inner_val` на `(hp_train, inner_val)`; затем `build_logreg_pipeline(C)` и
    `fit` на `fit_idx` (сезон: `tr ∪ iv ∪ cal`; финал: все строки) без прогрева;
  - `FitResult(..., n_rows_train=len(fit_idx_без_прогрева), n_rows_inner_val=len(inner_val))`;
  - калибратор: `CalibratorFit(method=config.calibration.method.value, calibration_skipped=True,
    n_calibration=0, seed=config.random_seed, calibrator=_IdentityCalibrator())` — импорт
    `_IdentityCalibrator` из `calibrate.py` сделать публичным именем `IdentityCalibrator`
    (переименование, все места) или добавить в `calibrate.py` функцию
    `identity_calibrator(method, seed) -> CalibratorFit` — выбрать второе, это одна точка;
  - матрица: `X_task = X[features]` при `features is not None`, иначе `X`; срезы отчёта
    (`_slice_masks`) получают полный `X` (им нужны `*_prior_games_count`).

- [ ] **Step 1: тесты (падают):**
  - `resolve_models("both", "home_win") == ["logreg"]`; `resolve_models("lgbm", "home_win")` →
    `ConfigError`; `resolve_models("both", "over_5_5") == ["logreg", "lgbm"]`;
  - смоук `run_training(task="both", model="both")` на синтетике: пар ровно 3, нет
    `models/home_win/lgbm`; `final/metadata.json` `home_win` — `model_features` == список из
    конфига, `calibration_skipped is True`; у `over_5_5` — все колонки;
  - прогрев: в `season_*/metadata.json` `home_win` `train_days.min` ≥ первого дня второго сезона;
  - `predict` на артефакте подаёт ровно `model_features` (модель-заглушка не нужна: реальный
    `home_win/logreg` из смоук-прогона, проверить `n_features_in_` и равенство вероятностей
    ручному `predict_proba(X[model_features])`);
  - `predict` на `metadata.json` без `model_features` — `KeyError` (Review Focus 4);
  - CLI: `train --task home_win --model lgbm` → код выхода ≠ 0 с сообщением; `predict/promote/
    publish-predictions --task home_win --model lgbm` — отказ.

- [ ] **Step 2:** запустить — FAIL.

- [ ] **Step 3: реализация** по схеме выше. `run_training`: внешний цикл по задачам, внутренний —
  по `resolve_models(model, task_name)`; `validate_run_id_override` считает пары так же.
  `dry_run_training` — то же. Ветка `home_win` в цикле сезонов и в финале вызывает `_fit_home_win`
  и `identity_calibrator`; остальное (метрики, Elo, отчёт, артефакты) общее.
  `_build_season_metadata`/`_build_final_metadata` — добавить `"model_features": list(X_task.columns)`.
  `predict_runner.run_predict`: `X = X[model_metadata["model_features"]]` перед `predict_raw_proba`.
  CLI: проверка «семейство положено задаче» через `resolve_models` (для `train`) и через
  `TASK_MODEL_FAMILIES` (для `predict`/`promote`/`publish-predictions`), с `ConfigError`/выходом 2.
  `Makefile`: `MODEL ?= logreg`.

- [ ] **Step 4:** удалить тесты/ветки, обучающие `home_win`+`lgbm` (список файлов выше); тесты
  `over_5_5`+`lgbm` оставить.

- [ ] **Step 5:** `make ci-local` — зелёный; коммит
  `Задача 68: home_win — logreg на своих признаках, без прогрева и Platt; lgbm для home_win удалён`.

---

### Task 4: гейт `home_win` по ECE и полоса шума в таблице калибровки

**Files:**
- Modify: `modeling/acceptance.py` (`TaskModelEval`, `TaskBaselineVerdict`, `evaluate_baseline_gate`,
  `format_baseline_summary_section`, `_evals_from_outcomes`)
- Modify: `modeling/train_runner.py` (`_TaskModelOutcome` — ECE модели и эталона)
- Modify: `modeling/metrics.py` (`calibration_bins` — `noise_2se`, `outside_noise`)
- Test: `tests/test_modeling_acceptance.py`, `tests/test_modeling_metrics.py`

**Interfaces:**
- Produces: `TaskModelEval.model_ece: float`, `TaskModelEval.benchmark_ece: float`;
  `TaskBaselineVerdict.reason: str` (пусто при проходе).
- Produces: строки `calibration_bins` с ключами `noise_2se`, `outside_noise`.

- [ ] **Step 1: тесты (падают):**
  - `evaluate_baseline_gate`: `home_win` с log-loss ниже Elo и `model_ece == benchmark_ece` —
    `passed`; с `model_ece > benchmark_ece` — `not passed`, `reason` содержит `ece`; с log-loss
    не ниже — `reason` содержит `log_loss`; `over_5_5` против константы — ECE не проверяется;
  - `format_baseline_summary_section` выводит `reason`;
  - `calibration_bins`: на корзине с `n=100`, средним прогнозом 0.5 и фактом 0.6 —
    `noise_2se ≈ 1.96·√(0.25/100) = 0.098`, `outside_noise is True` при факте 0.65 и `False` при 0.55
    (в долях, не в п.п.; как остальные поля таблицы).

- [ ] **Step 2:** запустить — FAIL.

- [ ] **Step 3: реализация.** В гейте: `passed_ll = model_ll + BASELINE_STRICT_EPS < benchmark_ll`;
  для `benchmark == "elo"` дополнительно `passed_ece = model_ece <= benchmark_ece`;
  `reason` — `"log_loss not below elo"` / `"ece above elo"` / оба через `; `. `calibration_bins`:
  после построения таблицы добавить колонки `noise_2se = 1.96*sqrt(p̄(1−p̄)/n)` и
  `outside_noise = |fact − p̄| > noise_2se` (имена колонок среднего прогноза и факта — как в
  `reliability_table`). Docstring гейта и `calibration_bins` обновить.

- [ ] **Step 4:** `make ci-local` — зелёный; коммит
  `Задача 68: гейт home_win — log-loss и ECE не хуже Elo; полоса шума в таблице калибровки`.

---

### Task 5: retrain снова включён, документация обучения

**Files:**
- Modify: `docker-compose.yml:100-112` (снять «Отключён до Задачи 68»)
- Modify: `pipeline/scheduled_retrain.py:116,168` (`over_5_5`-комментарий не трогать; подсказка `MODEL=logreg`)
- Modify: `docs/modeling_training.md` §2 (таблица YAML: `tasks.*.features`), §2a (гейт `home_win`,
  `model_features`, прогрев, без Platt), §7 (`--model` для `home_win` — только `logreg`,
  `model_features`), §9 (снять баннер, `MODEL=logreg`, `latest` двигает человек)
- Test: `tests/test_scheduled_retrain.py` (если проверяет текст подсказки)

- [ ] **Step 1:** правки текста по списку выше; в §2a написать, что критерий карточки
  «≤ 2 п.п. в корзинах» заменён решением человека 2026-10-11 (шум корзины ±4–8 п.п., Elo его не
  проходит) и почему нет Platt (ссылка на «Результаты Задачи 68»).
- [ ] **Step 2:** `make ci-local` — зелёный; коммит `Задача 68: retrain снова включён, документация`.

---

### Task 6: реальный прогон, результаты, закрытие задачи

Выполняет оркестратор (нужна живая БД), не субагент.

- [ ] **Step 1:** `PG_USER=postgres make all-tests` — зелёный (изменён загрузочный SQL сборщика).
- [ ] **Step 2:** `.venv/bin/python -m modeling.cli build-dataset --mode train --min-prior-games 5`
  (рабочая БД `postgres`); проверить 6049 строк, колонки `*_shot_share_ewm` без NaN.
- [ ] **Step 3:** `.venv/bin/python -m modeling.cli train --config configs/modeling_default.yaml --task home_win`
  → `status: ok`? Снять из `metrics.json`: по сезонам константа / Elo сырой / Elo с кривой / модель;
  сумма; ДИ `model_minus_elo`; Brier/ECE; таблица корзин с `noise_2se`; срезы; выбранные C.
- [ ] **Step 4:** `make modeling-publish TASK=home_win MODEL=logreg` после
  `make modeling-promote TASK=home_win MODEL=logreg RUN_ID=<run_id>` — локально, проверить строки
  `game_predictions` (`task='home_win'`, `model='logreg'`).
- [ ] **Step 5:** `docs/modeling_training.md` — раздел «Результаты Задачи 68»: таблицы прогона и
  разведки (из спеки), выводы, ограничение (андердоги-хозяева у Elo), что гейт пройден/не пройден.
  Если гейт не пройден — остановиться и сообщить человеку (задача 🟨, retrain-правки Task 5 откатить).
- [ ] **Step 6:** закрытие по регламенту `CLAUDE.md` (реестр `plan/open_tasks.md` → `plan/archive/closed_tasks.md`,
  карточка → `plan/archive/tasks/task_model/`, `plan/tasks/task_model/README.md`, срез `origin/master`
  после `git fetch`, номер PR из `gh pr list --state open`); строку Global Constraints 8 в
  `CLAUDE.md` проверить на расхождение с БД (6568 сыгранных игр, 8 в 2026/27).
- [ ] **Step 7:** `make ci-local`; ревью ветки Opus; PR.
