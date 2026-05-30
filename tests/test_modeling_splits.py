"""Tests for modeling.splits walk-forward temporal splits (stage 4)."""

from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from modeling.config import ConfigError, HoldoutConfig, SplitConfig, SplitMethod
from modeling.splits import SplitError, build_walk_forward_splits, validate_metadata_parity


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


def _synthetic_calendar_keys(
    *,
    start: str = "2018-10-01",
    n_days: int = 1200,
    games_per_day: int = 1,
) -> pd.DataFrame:
    """Two+ seasons of daily games; default one game per day for strict day gaps."""
    days = pd.date_range(start=start, periods=n_days, freq="D")
    rows: list[dict[str, object]] = []
    game_id = 1
    for day in days:
        for _ in range(games_per_day):
            rows.append({"day": day, "game_id": game_id})
            game_id += 1
    return pd.DataFrame(rows)


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
    def test_n_test_windows_below_minimum_raises(self) -> None:
        with self.assertRaises(ValidationError):
            SplitConfig.model_validate(
                {
                    "method": SplitMethod.month,
                    "n_test_windows": 4,
                    "inner_val_games": 300,
                    "calibration_games": 300,
                    "holdout": {"fraction": 0.15},
                }
            )

    def test_inner_val_games_below_minimum_raises(self) -> None:
        with self.assertRaises(ValidationError):
            SplitConfig.model_validate(
                {
                    "method": SplitMethod.month,
                    "n_test_windows": 5,
                    "inner_val_games": 299,
                    "calibration_games": 300,
                    "holdout": {"fraction": 0.15},
                }
            )

    def test_calibration_games_below_minimum_raises(self) -> None:
        with self.assertRaises(ValidationError):
            SplitConfig.model_validate(
                {
                    "method": SplitMethod.month,
                    "n_test_windows": 5,
                    "inner_val_games": 300,
                    "calibration_games": 299,
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


if __name__ == "__main__":
    unittest.main()
