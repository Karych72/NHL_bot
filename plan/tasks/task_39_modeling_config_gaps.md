# Задача 39. Два пробела конфига моделирования

**Статус:** ✅ выполнена (2026-09-28, ветка `sdd-task-39`, PR #37) · **Блок:** трек B · **Надобность:** 🟠 подтверждённый долг
**Область:** `configs/modeling_default.yaml`, `configs/modeling_smoke.yaml`; тест на согласованность имён.
**Порядок:** первая в треке B: **39** → 40 → (21) → 22 → 26.
**Реестр:** [`../open_tasks.md`](../open_tasks.md), раздел «Трек B»; детали — `docs/modeling_training.md` §7.

## Кратко

Первый реальный прогон обучения (Задача 15) вскрыл две ошибки конфигов.
(1) `models.lgbm.monotone` в обоих YAML ссылается на несуществующие фичи
(`diff_gf_roll_mean_*`, `diff_ga_roll_mean_*`, `diff_goal_diff_roll_mean_{10,20}`,
`sum_gf_*`, `sum_ga_*`), и `build_monotone_constraints` падает с `ConfigError` — lgbm под
штатным конфигом не обучается вовсе. (2) В `modeling_default.yaml`
`calibration.min_samples: 500` при `split.calibration_games: 300` — каждый калибровочный
блок меньше порога, `fit_calibrator` молча пропускает калибровку на всех фолдах и кладёт
identity-калибратор.

## Зачем и на что влияет

- **Зачем.** Пока это не исправлено, любое суждение о качестве модели (Задача 40)
  некорректно: lgbm не участвует в сравнении, а гейт для logreg считался на сырых,
  некалиброванных вероятностях. Задача 39 не про то, чтобы модель заработала, — про то,
  чтобы сравнение вообще было честным.
- **На что влияет.** Только конфиги и один тест, запрещающий им снова разъехаться
  с реальным манифестом фич. Артефакты прошлых прогонов становятся несопоставимы
  с новыми — ручной `latest` из Задачи 15 теряет смысл.
- **Если не делать.** Задача 40 начнётся с неверной точки отсчёта.

## Степень важности

🟠 **Подтверждённый дефект с известным исправлением.** Небольшая по объёму, но обязательная
перед 40; для релиза статистического бота не нужна.

## Подробно

### Текущее состояние (сверено 2026-09-22)

- Реальные имена rolling-фич (`modeling/dataset_builder/features.py`):
  `ROLLING_BASE_FIELDS` = `goals_for, goals_against, shots_for, shots_against, pim_for,
  pim_against, power_play_percentage_for, power_play_percentage_against` →
  `<field>_roll_mean_<w>` на каждом окне; производные `goal_diff_roll_mean_5` и
  `pace_sum_roll_mean_5` существуют **только** на окне 5 (`features.py:73-93`).
  На уровне матча — `home_*`, `away_*`, `diff_*` (home − away), `sum_*`.
- Блок `monotone` в обоих конфигах: `home_win` → `diff_goal_diff_roll_mean_{5,10,20}: 1`,
  `diff_gf_roll_mean_{5,10,20}: 1`, `diff_ga_roll_mean_{5,10,20}: -1`; `over_5_5` →
  `sum_gf_roll_mean_*: 1`, `sum_ga_roll_mean_*: 1`. Из них существует только
  `diff_goal_diff_roll_mean_5`.
- `modeling_default.yaml`: `split.calibration_games: 300`, `calibration.min_samples: 500`,
  `method: isotonic`. `modeling_smoke.yaml` (комментарий у `min_samples`) сам формулирует
  правило: `min_samples` не должен превышать `split.calibration_games` (там 50 = 50).
- Подтверждено реальным прогоном: `artifacts/models/home_win/logreg/latest/metadata.json`
  — `calibration_skipped: true, n_calibration: 300`.

### Что нужно сделать

1. **Имена в `monotone`** заменить на реальные и оставить только существующие окна:
   `diff_goals_for_roll_mean_{5,10,20}: 1`, `diff_goals_against_roll_mean_{5,10,20}: -1`,
   `diff_goal_diff_roll_mean_5: 1` (10 и 20 убрать — их нет); для `over_5_5` —
   `sum_goals_for_roll_mean_*: 1`, `sum_goals_against_roll_mean_*: 1`. Знаки — предметное
   решение, обосновать в отчёте; лишних ограничений не добавлять.
2. **`calibration.min_samples ≤ split.calibration_games`** в `modeling_default.yaml`
   (300 или меньше). Учесть наблюдение Задачи 15: изотоническая калибровка на блоке
   в 50 строк ухудшила результат; на 300 строках смотреть по отчёту (ECE до/после),
   а не принимать на веру. Смена метода калибровки — отдельное решение, не здесь.
3. **Тест на согласованность:** каждый ключ `models.lgbm.monotone` в обоих конфигах
   совпадает хотя бы с одной колонкой манифеста фич (синтетический датасет
   `tests/_modeling_fixtures.py` носит те же имена) и
   `calibration.min_samples ≤ split.calibration_games`.
4. **Реальный прогон** `python -m modeling.cli train --config configs/modeling_default.yaml`
   без `--set models.lgbm.monotone={}`: lgbm обучается, в `metadata.json` каждого
   артефакта `calibration_skipped: false`.
5. `docs/modeling_training.md` §7: абзацы «Known config gap» удалить или заменить ссылкой
   на закрытие (документация — часть задачи).

### Критерии приёмки (из реестра)

- Имена в `models.lgbm.monotone` совпадают с реальными колонками фич; lgbm обучается
  без `--set models.lgbm.monotone={}`.
- `calibration.min_samples ≤ split.calibration_games`.
- В `metadata.json` прогона `calibration_skipped: false`.

### Проверка

- `make ci-local`; реальный `train` под `modeling_default.yaml` — фрагмент `metadata.json`
  и строка отчёта lgbm в отчёт задачи.

### Подводные камни

- Полная сетка lgbm — 3^7 = 2187 обучений на задачу на окно; прогон долгий. Сам факт
  «обучается» проверяется на `modeling_smoke.yaml`; `calibration_skipped: false` на
  профиле 300/300/5 — только на default.
- Не трогать `features.py` ради конфига: если нужны `goal_diff` на окнах 10/20 —
  это Задача 40.
