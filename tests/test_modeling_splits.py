"""Tests for modeling.splits season-by-season temporal splits (Задача 66)."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from pydantic import ValidationError

from modeling.config import ConfigError, SplitConfig
from modeling.splits import (
    SplitError,
    _assert_strictly_before,
    build_season_splits,
    validate_metadata_parity,
)
from tests._modeling_fixtures import synthetic_calendar_keys, synthetic_season_id

# Real NHL game days carry 3-11 games, never a constant -- this cycle mimics that
# irregularity so train/inner_val/calibration boundaries can land inside a day.
IRREGULAR_GAMES_PER_DAY = [7, 3, 11, 5, 9, 4, 8, 6, 10]

S0, S1, S2, S3 = (synthetic_season_id(i) for i in range(4))


def _config(**overrides: object) -> SplitConfig:
    payload: dict[str, object] = {
        "test_seasons": [S2, S3],
        "inner_val_games": 50,
        "calibration_games": 40,
    }
    payload.update(overrides)
    return SplitConfig.model_validate(payload)


def _four_seasons(**kwargs: object) -> pd.DataFrame:
    """4 seasons x 100 days, one game per day unless overridden."""
    return synthetic_calendar_keys(n_days=400, days_per_season=100, **kwargs)  # type: ignore[arg-type]


def _ids(keys: pd.DataFrame, idx: np.ndarray) -> list[int]:
    ordered = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)
    return ordered.iloc[idx]["game_id"].tolist()


class TestSeasonBoundaries(unittest.TestCase):
    def test_test_block_is_exactly_the_checked_season(self) -> None:
        keys = _four_seasons()
        splits = build_season_splits(keys, _config())

        self.assertEqual([w.season_id for w in splits.windows], [S2, S3])
        for window in splits.windows:
            expected = keys.loc[keys["season_id"] == window.season_id, "game_id"].tolist()
            self.assertEqual(_ids(keys, window.test_idx), expected)

    def test_train_valid_calibration_are_all_earlier_rows(self) -> None:
        keys = _four_seasons()
        config = _config()
        splits = build_season_splits(keys, config)

        for window in splits.windows:
            earlier = keys.loc[keys["season_id"] < window.season_id, "game_id"].tolist()
            joined = (
                _ids(keys, window.train_idx)
                + _ids(keys, window.inner_val_idx)
                + _ids(keys, window.calibration_idx)
            )
            self.assertEqual(joined, earlier)
            self.assertEqual(window.inner_val_size, config.inner_val_games)
            self.assertEqual(window.calibration_size, config.calibration_games)
            self.assertEqual(window.train_size, len(earlier) - 90)

    def test_rows_after_last_checked_season_are_not_used(self) -> None:
        keys = _four_seasons()
        splits = build_season_splits(keys, _config(test_seasons=[S1, S2]))

        used = set()
        for window in splits.windows:
            for idx in (window.train_idx, window.inner_val_idx, window.calibration_idx, window.test_idx):
                used.update(_ids(keys, idx))
        self.assertTrue(used.isdisjoint(keys.loc[keys["season_id"] == S3, "game_id"]))

    def test_blocks_ordered_and_test_windows_disjoint(self) -> None:
        keys = _four_seasons(games_per_day=IRREGULAR_GAMES_PER_DAY)
        splits = build_season_splits(keys, _config())

        for window in splits.windows:
            self.assertLess(window.train_idx.max(), window.inner_val_idx.min())
            self.assertLess(window.inner_val_idx.max(), window.calibration_idx.min())
            self.assertLess(window.calibration_idx.max(), window.test_idx.min())
        first, second = (set(_ids(keys, w.test_idx)) for w in splits.windows)
        self.assertTrue(first.isdisjoint(second))

    def test_block_boundary_can_fall_inside_a_day(self) -> None:
        keys = _four_seasons(games_per_day=IRREGULAR_GAMES_PER_DAY)
        splits = build_season_splits(keys, _config())
        days = keys.sort_values(["day", "game_id"], kind="mergesort").reset_index(drop=True)["day"]

        straddling = [
            w.season_id
            for w in splits.windows
            if days.iloc[w.train_idx.max()] == days.iloc[w.inner_val_idx.min()]
            or days.iloc[w.inner_val_idx.max()] == days.iloc[w.calibration_idx.min()]
        ]
        self.assertTrue(straddling, "fixture no longer exercises a same-day block boundary")

    def test_shuffled_input_yields_identical_splits(self) -> None:
        keys = _four_seasons(games_per_day=IRREGULAR_GAMES_PER_DAY)
        shuffled = keys.sample(frac=1.0, random_state=0).reset_index(drop=True)

        base = build_season_splits(keys, _config())
        perm = build_season_splits(shuffled, _config())

        for left, right in zip(base.windows, perm.windows):
            for attr in ("train_idx", "inner_val_idx", "calibration_idx", "test_idx"):
                np.testing.assert_array_equal(getattr(left, attr), getattr(right, attr))

    def test_log_dict_reports_sizes_and_days(self) -> None:
        keys = _four_seasons()
        log = build_season_splits(keys, _config()).to_log_dict()

        self.assertEqual(log["n_windows"], 2)
        first = log["windows"][0]
        self.assertEqual(first["season_id"], S2)
        self.assertEqual(first["test_size"], 100)
        self.assertEqual(first["test_days"][0], str(keys["day"].iloc[200].date()))


class TestSplitErrors(unittest.TestCase):
    def test_missing_checked_season_raises(self) -> None:
        with self.assertRaises(SplitError) as ctx:
            build_season_splits(_four_seasons(), _config(test_seasons=[S2, 29992000]))
        self.assertIn("29992000", str(ctx.exception))

    def test_too_little_history_before_season_raises(self) -> None:
        # S1 is preceded by exactly 100 rows: 59 + 40 + 1 fit, 60 + 40 + 1 do not.
        build_season_splits(_four_seasons(), _config(test_seasons=[S1], inner_val_games=59))
        with self.assertRaises(SplitError) as ctx:
            build_season_splits(_four_seasons(), _config(test_seasons=[S1], inner_val_games=60))
        message = str(ctx.exception)
        self.assertIn(str(S1), message)
        self.assertIn("101", message)

    def test_first_season_has_no_history(self) -> None:
        with self.assertRaises(SplitError):
            build_season_splits(_four_seasons(), _config(test_seasons=[S0]))

    def test_keys_without_season_id_raise(self) -> None:
        with self.assertRaises(SplitError) as ctx:
            build_season_splits(_four_seasons().drop(columns=["season_id"]), _config())
        self.assertIn("season_id", str(ctx.exception))


class TestConfigFailFast(unittest.TestCase):
    def test_test_seasons_must_be_non_empty(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            _config(test_seasons=[])
        self.assertIn("test_seasons", str(ctx.exception))

    def test_test_seasons_must_be_strictly_increasing(self) -> None:
        for seasons in ([S3, S2], [S2, S2]):
            with self.subTest(seasons=seasons):
                with self.assertRaises(ValidationError) as ctx:
                    _config(test_seasons=seasons)
                self.assertIn("strictly increasing", str(ctx.exception))

    def test_non_positive_block_sizes_rejected(self) -> None:
        for key in ("inner_val_games", "calibration_games"):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    _config(**{key: 0})


class TestEmbargoComment(unittest.TestCase):
    def test_splits_module_documents_embargo_policy(self) -> None:
        source = Path("modeling/splits.py").read_text(encoding="utf-8")
        self.assertIn("embargo", source.lower())
        self.assertIn("shift(1)", source)


class TestMetadataParity(unittest.TestCase):
    def test_yaml_metadata_mismatch_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            validate_metadata_parity({"features_hash": "aaa"}, {"features_hash": "bbb"})
        self.assertIn("features_hash", str(ctx.exception))

    def test_mismatch_surfaces_from_build_season_splits(self) -> None:
        with self.assertRaises(ConfigError):
            build_season_splits(
                _four_seasons(),
                _config(),
                yaml_reference={"features_hash": "aaa"},
                metadata={"features_hash": "bbb"},
            )


class TestShippedConfigsGeometry(unittest.TestCase):
    def test_default_and_smoke_split_sections_parse(self) -> None:
        for name in ("modeling_default.yaml", "modeling_smoke.yaml"):
            with self.subTest(config=name):
                raw = yaml.safe_load(Path("configs", name).read_text(encoding="utf-8"))
                config = SplitConfig.model_validate(raw["split"])
                self.assertGreaterEqual(len(config.test_seasons), 2)

    def test_default_checks_three_whole_seasons(self) -> None:
        raw = yaml.safe_load(Path("configs/modeling_default.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["split"]["test_seasons"], [20232024, 20242025, 20252026])


class TestOrderCheckCatchesRealViolation(unittest.TestCase):
    """The positional ``_assert_strictly_before`` must still raise on a genuine violation."""

    def test_position_violations_raise_split_error(self) -> None:
        days = pd.Series(pd.date_range("2020-01-01", periods=10, freq="D"))
        cases = (
            ("overlapping positions", np.array([3, 4, 7]), np.array([6, 8, 9])),
            ("reversed blocks", np.array([8, 9]), np.array([0, 1])),
        )
        for label, earlier_idx, later_idx in cases:
            with self.subTest(label=label):
                with self.assertRaises(SplitError) as ctx:
                    _assert_strictly_before(days, earlier_idx, later_idx, label)
                self.assertIn(label, str(ctx.exception))

    def test_correctly_ordered_positions_do_not_raise(self) -> None:
        days = pd.Series(pd.date_range("2020-01-01", periods=10, freq="D"))
        _assert_strictly_before(days, np.array([0, 1, 2]), np.array([3, 4, 5]), "should not raise")


if __name__ == "__main__":
    unittest.main()
