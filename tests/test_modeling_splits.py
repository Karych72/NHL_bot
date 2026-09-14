"""Tests for modeling.splits walk-forward temporal splits (stage 11)."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from pydantic import ValidationError

from modeling.config import ConfigError, HoldoutConfig, SplitConfig, SplitMethod
from modeling.splits import (
    SplitError,
    _assert_strictly_before,
    build_walk_forward_splits,
    validate_metadata_parity,
)
from tests._modeling_fixtures import synthetic_calendar_keys

# Real NHL game days carry 3-11 games, never a constant -- this cycle mimics
# that irregularity (avg ~7, matching the real calendar's ~7.2). Every
# pre-Задача 32 split test used games_per_day=1, so a block boundary always
# happened to land exactly on a day boundary and never exercised the bug.
IRREGULAR_GAMES_PER_DAY = [7, 3, 11, 5, 9, 4, 8, 6, 10]


def _default_split_config(**overrides: object) -> SplitConfig:
    payload = {
        "method": SplitMethod.month,
        "n_test_windows": 5,
        "inner_val_games": 300,
        "calibration_games": 300,
        "holdout": HoldoutConfig(fraction=0.15),
    }
    payload.update(overrides)
    return SplitConfig.model_validate(payload)


def _synthetic_calendar_keys(**kwargs: object) -> pd.DataFrame:
    return synthetic_calendar_keys(**kwargs)  # type: ignore[arg-type]


def _game_ids_for(keys: pd.DataFrame) -> list[int]:
    sorted_keys = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    return sorted_keys["game_id"].tolist()


def _block_game_ids(keys: pd.DataFrame, idx: pd.Index | list[int]) -> list[int]:
    sorted_keys = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    return sorted_keys.iloc[list(idx)]["game_id"].tolist()


class TestWalkForwardMonotonicity(unittest.TestCase):
    def test_calendar_month_blocks_are_temporally_ordered(self) -> None:
        keys = _synthetic_calendar_keys()
        config = _default_split_config()
        splits = build_walk_forward_splits(keys, config)
        sorted_keys = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
        days = sorted_keys["day"]
        holdout_min = days.iloc[splits.holdout.holdout_idx].min()

        self.assertEqual(len(splits.windows), config.n_test_windows)
        for window in splits.windows:
            self.assertLess(days.iloc[window.train_idx].max(), days.iloc[window.inner_val_idx].min())
            self.assertLess(days.iloc[window.inner_val_idx].max(), days.iloc[window.calibration_idx].min())
            self.assertLess(days.iloc[window.calibration_idx].max(), days.iloc[window.test_idx].min())
            self.assertLess(days.iloc[window.test_idx].max(), holdout_min)


class TestHoldoutIsolation(unittest.TestCase):
    def test_holdout_does_not_overlap_walk_forward_blocks(self) -> None:
        keys = _synthetic_calendar_keys()
        config = _default_split_config()
        splits = build_walk_forward_splits(keys, config)
        sorted_keys = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
        holdout_ids = set(sorted_keys.iloc[splits.holdout.holdout_idx]["game_id"].tolist())

        for window in splits.windows:
            for block in (
                window.train_idx,
                window.inner_val_idx,
                window.calibration_idx,
                window.test_idx,
            ):
                block_ids = set(sorted_keys.iloc[block]["game_id"].tolist())
                self.assertFalse(holdout_ids.intersection(block_ids))


class TestMinimumBlockSizes(unittest.TestCase):
    def test_inner_val_and_calibration_meet_config_minimums(self) -> None:
        keys = _synthetic_calendar_keys()
        config = _default_split_config()
        splits = build_walk_forward_splits(keys, config)

        for window in splits.windows:
            self.assertGreaterEqual(window.inner_val_size, config.inner_val_games)
            self.assertGreaterEqual(window.calibration_size, config.calibration_games)
            self.assertGreater(window.test_size, 0)
            self.assertGreater(window.train_size, 0)


class TestStableSorting(unittest.TestCase):
    def test_shuffled_input_yields_identical_splits(self) -> None:
        keys = _synthetic_calendar_keys()
        config = _default_split_config()
        shuffled = keys.sample(frac=1.0, random_state=0).reset_index(drop=True)

        base = build_walk_forward_splits(keys, config)
        perm = build_walk_forward_splits(shuffled, config)
        sorted_keys = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)

        for left, right in zip(base.windows, perm.windows):
            for attr in ("train_idx", "inner_val_idx", "calibration_idx", "test_idx"):
                self.assertEqual(
                    sorted_keys.iloc[getattr(left, attr)]["game_id"].tolist(),
                    sorted_keys.iloc[getattr(right, attr)]["game_id"].tolist(),
                    msg=attr,
                )
        self.assertEqual(
            sorted_keys.iloc[base.holdout.holdout_idx]["game_id"].tolist(),
            sorted_keys.iloc[perm.holdout.holdout_idx]["game_id"].tolist(),
        )


class TestConfigFailFast(unittest.TestCase):
    """Sanity (positive-only) floors on split.* (Задача 14, ruling Р2).

    Actual data-volume adequacy is no longer enforced here — it moved to the
    guard in ``build_walk_forward_splits`` (see ``TestMinimumHistoryGuard``
    below). These tests only assert that non-positive values — zero and
    negative alike — are rejected.
    """

    def test_n_test_windows_below_minimum_raises(self) -> None:
        for value in (0, -1):
            with self.subTest(n_test_windows=value), self.assertRaises(ValidationError):
                SplitConfig.model_validate(
                    {
                        "method": SplitMethod.month,
                        "n_test_windows": value,
                        "inner_val_games": 300,
                        "calibration_games": 300,
                        "holdout": {"fraction": 0.15},
                    }
                )

    def test_inner_val_games_below_minimum_raises(self) -> None:
        for value in (0, -1):
            with self.subTest(inner_val_games=value), self.assertRaises(ValidationError):
                SplitConfig.model_validate(
                    {
                        "method": SplitMethod.month,
                        "n_test_windows": 5,
                        "inner_val_games": value,
                        "calibration_games": 300,
                        "holdout": {"fraction": 0.15},
                    }
                )

    def test_calibration_games_below_minimum_raises(self) -> None:
        for value in (0, -1):
            with self.subTest(calibration_games=value), self.assertRaises(ValidationError):
                SplitConfig.model_validate(
                    {
                        "method": SplitMethod.month,
                        "n_test_windows": 5,
                        "inner_val_games": 300,
                        "calibration_games": value,
                        "holdout": {"fraction": 0.15},
                    }
                )


class TestInsufficientHistory(unittest.TestCase):
    def test_short_history_raises_split_error(self) -> None:
        keys = _synthetic_calendar_keys(n_days=120)
        config = _default_split_config()
        with self.assertRaises(SplitError) as ctx:
            build_walk_forward_splits(keys, config)
        self.assertIn("not enough history", str(ctx.exception).lower())


class TestMinimumHistoryGuard(unittest.TestCase):
    """Задача 14: top-level data-volume guard fails fast with numbers, not a
    silent empty/degenerate split. Checks both ``method: month`` and
    ``method: fixed_games`` (SplitError message must contain both the
    required and the actual row counts)."""

    def test_month_method_guard_reports_required_and_actual_rows(self) -> None:
        keys = _synthetic_calendar_keys(n_days=100, games_per_day=1)
        config = _default_split_config()  # month, n_test_windows=5, 300/300, holdout 0.15
        with self.assertRaises(SplitError) as ctx:
            build_walk_forward_splits(keys, config)
        message = str(ctx.exception)
        # holdout = ceil(100 * 0.15) = 15
        # required_wf = inner_val(300) + calibration(300) + n_test_windows(5) * test(>=1) + train(>=1) = 606
        # required_total = holdout(15) + required_wf(606) = 621; actual = 100 rows total
        self.assertIn("621", message)
        self.assertIn("100", message)

    def test_fixed_games_method_guard_reports_required_and_actual_rows(self) -> None:
        keys = _synthetic_calendar_keys(n_days=100, games_per_day=1)
        config = SplitConfig.model_validate(
            {
                "method": SplitMethod.fixed_games,
                "n_test_windows": 5,
                "inner_val_games": 300,
                "calibration_games": 300,
                "outer_block_games": 700,
                "holdout": {"fraction": 0.15},
            }
        )
        with self.assertRaises(SplitError) as ctx:
            build_walk_forward_splits(keys, config)
        message = str(ctx.exception)
        # holdout = ceil(100 * 0.15) = 15
        # required_wf = n_test_windows(5) * outer_block_games(700) + train(>=1) = 3501
        # required_total = holdout(15) + required_wf(3501) = 3516; actual = 100 rows total
        self.assertIn("3516", message)
        self.assertIn("100", message)


class TestGuardIsNecessaryNotSufficient(unittest.TestCase):
    """Задача 14: clearing the row-count guard does not mean the geometry fits.

    Covers the late ``_window_from_tail`` check, which the guard must NOT
    subsume — it is the check that actually protects the scenario motivating
    this task — and doubles as evidence for the "necessary but not sufficient"
    claim made in ``modeling/splits.py``, both YAML configs and
    ``docs/modeling_training.md``.

    Season-shaped calendar: 200 days x 6 games = 1200 rows over 7 calendar
    months. holdout = ceil(200 * 0.15) = 30 days = 180 rows, leaving 1020
    walk-forward rows across 6 months. The default profile needs 606
    walk-forward rows, so the guard passes; but the earliest of its 5 test
    months has only 186 rows before it against inner_val(300) +
    calibration(300) = 600, so ``_window_from_tail`` raises instead.
    """

    def test_guard_passes_then_window_from_tail_raises(self) -> None:
        keys = _synthetic_calendar_keys(n_days=200, games_per_day=6)
        config = _default_split_config()
        with self.assertRaises(SplitError) as ctx:
            build_walk_forward_splits(keys, config)
        message = str(ctx.exception)
        # Distinguishing text of _window_from_tail: the test must not start
        # passing silently on the guard's own message instead.
        self.assertIn("window k=1: need 600 rows before test, have 186", message)
        self.assertNotIn("walk-forward rows after holdout cut", message)


class TestEmbargoComment(unittest.TestCase):
    def test_splits_module_documents_embargo_policy(self) -> None:
        source = Path("modeling/splits.py").read_text(encoding="utf-8")
        lowered = source.lower()
        self.assertIn("embargo", lowered)
        self.assertIn("shift(1)", source)
        self.assertIn("stage 4", lowered)


class TestMetadataParity(unittest.TestCase):
    def test_yaml_metadata_mismatch_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            validate_metadata_parity(
                {"features_hash": "aaa"},
                {"features_hash": "bbb"},
            )
        self.assertIn("features_hash", str(ctx.exception))


class TestSmokeProfileFitsSingleSeason(unittest.TestCase):
    """Задача 14: configs/modeling_smoke.yaml's geometry must actually build
    windows on a single-season-sized dataset (~1211 rows -- the pre-Задача 30
    `season_20252026/` size), not just pass the guard's row-count check.

    Scope, stated exactly: the calendar here is one game per day, so it covers
    ~40 calendar months rather than a real season's ~7, and `before_test` for
    k=1 comes out far above the 100 rows the smoke geometry needs. It therefore
    proves that the profile builds complete, non-degenerate windows -- not that
    it survives a real season's month *shape*. A denser season-shaped fixture
    (200 days x 6 games) cannot be used here: `_window_from_tail` slices
    `before_test` by row count while `_validate_splits` requires day-strict
    block boundaries, so with several games per day a block edge lands inside a
    calendar day and the build raises "train must end before inner_val". That
    defect predates Задача 14 and is tracked as Задача 32 in
    `plan/engineering/work_plan_2026-08-08.md`. Calendar-shape behaviour is
    covered instead by `TestGuardIsNecessaryNotSufficient`.
    """

    def test_smoke_profile_builds_expected_windows(self) -> None:
        raw = yaml.safe_load(Path("configs/modeling_smoke.yaml").read_text(encoding="utf-8"))
        config = SplitConfig.model_validate(raw["split"])
        keys = _synthetic_calendar_keys(n_days=1211, games_per_day=1)

        splits = build_walk_forward_splits(keys, config)

        self.assertEqual(len(splits.windows), config.n_test_windows)
        for window in splits.windows:
            self.assertGreaterEqual(window.inner_val_size, config.inner_val_games)
            self.assertGreaterEqual(window.calibration_size, config.calibration_games)
            self.assertGreater(window.test_size, 0)
            self.assertGreater(window.train_size, 0)


class TestFixedGamesMethod(unittest.TestCase):
    def test_fixed_games_builds_requested_windows(self) -> None:
        keys = _synthetic_calendar_keys(n_days=5000, games_per_day=1)
        config = SplitConfig.model_validate(
            {
                "method": SplitMethod.fixed_games,
                "n_test_windows": 5,
                "inner_val_games": 300,
                "calibration_games": 300,
                "outer_block_games": 700,
                "holdout": {"fraction": 0.15},
            }
        )
        splits = build_walk_forward_splits(keys, config)
        self.assertEqual(len(splits.windows), 5)


class TestIrregularGamesPerDayBothMethods(unittest.TestCase):
    """Задача 32: real calendars have several games on the same day, unevenly
    (3-11, see ``IRREGULAR_GAMES_PER_DAY``) -- unlike every other fixture in
    this module, which uses ``games_per_day=1`` and therefore never lands a
    block boundary inside a day. Before the fix, both methods raised
    ``SplitError`` ("train must end before inner_val") on this exact calendar
    because the old check compared calendar days, not positions.
    """

    def test_month_method_builds_on_irregular_calendar(self) -> None:
        keys = _synthetic_calendar_keys(n_days=1200, games_per_day=IRREGULAR_GAMES_PER_DAY)
        config = _default_split_config()
        splits = build_walk_forward_splits(keys, config)

        self.assertEqual(len(splits.windows), config.n_test_windows)
        for window in splits.windows:
            self.assertGreaterEqual(window.inner_val_size, config.inner_val_games)
            self.assertGreaterEqual(window.calibration_size, config.calibration_games)
            self.assertGreater(window.test_size, 0)
            self.assertGreater(window.train_size, 0)
            # Positional guarantee still holds even though block edges may
            # fall inside a shared calendar day (asserted separately below).
            self.assertLess(window.train_idx.max(), window.inner_val_idx.min())
            self.assertLess(window.inner_val_idx.max(), window.calibration_idx.min())
            self.assertLess(window.calibration_idx.max(), window.test_idx.min())

    def test_fixed_games_method_builds_on_irregular_calendar(self) -> None:
        keys = _synthetic_calendar_keys(n_days=1200, games_per_day=IRREGULAR_GAMES_PER_DAY)
        config = SplitConfig.model_validate(
            {
                "method": SplitMethod.fixed_games,
                "n_test_windows": 5,
                "inner_val_games": 300,
                "calibration_games": 300,
                "outer_block_games": 700,
                "holdout": {"fraction": 0.15},
            }
        )
        splits = build_walk_forward_splits(keys, config)

        self.assertEqual(len(splits.windows), 5)
        for window in splits.windows:
            self.assertLess(window.train_idx.max(), window.inner_val_idx.min())
            self.assertLess(window.inner_val_idx.max(), window.calibration_idx.min())
            self.assertLess(window.calibration_idx.max(), window.test_idx.min())

    def test_irregular_calendar_actually_straddles_a_day_boundary(self) -> None:
        """Guard the fixture itself: prove this calendar shape genuinely puts
        a block edge inside a shared day, so the two tests above exercise the
        Задача 32 scenario rather than accidentally aligning on day borders."""
        keys = _synthetic_calendar_keys(n_days=1200, games_per_day=IRREGULAR_GAMES_PER_DAY)
        config = _default_split_config()
        splits = build_walk_forward_splits(keys, config)
        sorted_keys = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
        days = sorted_keys["day"]

        straddles = any(
            days.iloc[window.train_idx].max() == days.iloc[window.inner_val_idx].min()
            or days.iloc[window.inner_val_idx].max() == days.iloc[window.calibration_idx].min()
            or days.iloc[window.calibration_idx].max() == days.iloc[window.test_idx].min()
            for window in splits.windows
        )
        self.assertTrue(straddles, "fixture should straddle a day boundary at least once")


class TestOrderCheckCatchesRealViolation(unittest.TestCase):
    """Задача 32: the positional rewrite of ``_assert_strictly_before`` must
    still raise ``SplitError`` on a genuine ordering violation -- proof the
    rewrite did not degrade into a no-op that accepts any position order.
    """

    def test_overlapping_positions_raise_split_error(self) -> None:
        days = pd.Series(pd.date_range("2020-01-01", periods=10, freq="D"))
        # earlier block's max position (7) is >= later block's min position
        # (6): a genuine ordering violation (block reuses/precedes rows the
        # "later" block already claims).
        earlier_idx = np.array([3, 4, 7])
        later_idx = np.array([6, 8, 9])
        with self.assertRaises(SplitError) as ctx:
            _assert_strictly_before(days, earlier_idx, later_idx, "synthetic violation")
        self.assertIn("synthetic violation", str(ctx.exception))

    def test_reversed_blocks_raise_split_error(self) -> None:
        days = pd.Series(pd.date_range("2020-01-01", periods=10, freq="D"))
        # "earlier" block is entirely chronologically after "later" -- the
        # starkest possible violation.
        earlier_idx = np.array([8, 9])
        later_idx = np.array([0, 1])
        with self.assertRaises(SplitError):
            _assert_strictly_before(days, earlier_idx, later_idx, "reversed blocks")

    def test_correctly_ordered_positions_do_not_raise(self) -> None:
        days = pd.Series(pd.date_range("2020-01-01", periods=10, freq="D"))
        earlier_idx = np.array([0, 1, 2])
        later_idx = np.array([3, 4, 5])
        _assert_strictly_before(days, earlier_idx, later_idx, "should not raise")


if __name__ == "__main__":
    unittest.main()
