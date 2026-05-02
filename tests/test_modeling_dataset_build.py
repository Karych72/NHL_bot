"""Dataset builder contract tests: anti-leakage, parity, and quality."""

from __future__ import annotations

import unittest

import pandas as pd

from modeling.dataset_builder.assemble import apply_cold_start_policy
from modeling.dataset_builder.features import (
    build_match_feature_snapshots,
    compute_team_rolling_features,
)
from modeling.dataset_builder.schema import (
    assert_feature_parity,
    build_feature_manifest,
    feature_columns_from_df,
    features_hash,
)
from modeling.dataset_builder.team_game_facts import build_team_game_facts
from modeling.dataset_builder.validate import validate_or_raise


def _history_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # day 1
            {"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20, "team_id": 10, "goals": 2, "shots": 25, "pim": 6, "power_play_percentage": 20, "power_play_goals": 1, "power_play_opportunities": 5, "face_off_win_percentage": 49, "blocked": 10, "takeaways": 4, "giveaways": 7, "hits": 15},
            {"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20, "team_id": 20, "goals": 1, "shots": 20, "pim": 8, "power_play_percentage": 0, "power_play_goals": 0, "power_play_opportunities": 4, "face_off_win_percentage": 51, "blocked": 12, "takeaways": 5, "giveaways": 9, "hits": 11},
            # day 2
            {"game_id": 2, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 10, "away_team_id": 30, "team_id": 10, "goals": 4, "shots": 28, "pim": 2, "power_play_percentage": 33, "power_play_goals": 1, "power_play_opportunities": 3, "face_off_win_percentage": 55, "blocked": 9, "takeaways": 6, "giveaways": 8, "hits": 17},
            {"game_id": 2, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 10, "away_team_id": 30, "team_id": 30, "goals": 3, "shots": 27, "pim": 4, "power_play_percentage": 25, "power_play_goals": 1, "power_play_opportunities": 4, "face_off_win_percentage": 45, "blocked": 7, "takeaways": 4, "giveaways": 5, "hits": 9},
            {"game_id": 3, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 20, "away_team_id": 40, "team_id": 20, "goals": 2, "shots": 24, "pim": 6, "power_play_percentage": 20, "power_play_goals": 1, "power_play_opportunities": 5, "face_off_win_percentage": 52, "blocked": 10, "takeaways": 3, "giveaways": 8, "hits": 12},
            {"game_id": 3, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 20, "away_team_id": 40, "team_id": 40, "goals": 1, "shots": 18, "pim": 10, "power_play_percentage": 0, "power_play_goals": 0, "power_play_opportunities": 3, "face_off_win_percentage": 48, "blocked": 13, "takeaways": 2, "giveaways": 11, "hits": 10},
            # same day as target (must not be used as history)
            {"game_id": 9, "day": "2026-01-03", "season_id": 20252026, "home_team_id": 10, "away_team_id": 99, "team_id": 10, "goals": 9, "shots": 40, "pim": 20, "power_play_percentage": 50, "power_play_goals": 2, "power_play_opportunities": 4, "face_off_win_percentage": 60, "blocked": 5, "takeaways": 8, "giveaways": 2, "hits": 19},
            {"game_id": 9, "day": "2026-01-03", "season_id": 20252026, "home_team_id": 10, "away_team_id": 99, "team_id": 99, "goals": 1, "shots": 10, "pim": 5, "power_play_percentage": 0, "power_play_goals": 0, "power_play_opportunities": 2, "face_off_win_percentage": 40, "blocked": 20, "takeaways": 1, "giveaways": 1, "hits": 5},
        ]
    )


class TestModelingDatasetBuild(unittest.TestCase):
    def test_two_team_rows_or_drop(self):
        broken = _history_df().copy()
        broken = broken[~((broken["game_id"] == 2) & (broken["team_id"] == 30))]
        team_facts, report = build_team_game_facts(broken)
        self.assertFalse((team_facts["game_id"] == 2).any())
        self.assertTrue(any(item["game_id"] == 2 for item in report["dropped_games"]))

    def test_no_same_game_leakage(self):
        team_facts, _ = build_team_game_facts(_history_df())
        rolling = compute_team_rolling_features(team_facts, [5])
        targets = pd.DataFrame(
            [
                {"game_id": 100, "day": "2026-01-04", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20},
            ]
        )
        snapshots = build_match_feature_snapshots(targets, rolling)
        snapshots["feature_set_version"] = "v1"
        snapshots["dataset_built_at"] = "2026-01-04T00:00:00Z"
        snapshots["low_history_confidence"] = 0
        snapshots["quality_warnings"] = ""
        validate_or_raise("predict", snapshots, feature_columns=feature_columns_from_df(snapshots))
        self.assertNotEqual(int(snapshots.loc[0, "home_hist_game_id"]), 100)
        self.assertNotEqual(int(snapshots.loc[0, "away_hist_game_id"]), 100)

    def test_strict_past_only_by_day(self):
        team_facts, _ = build_team_game_facts(_history_df())
        rolling = compute_team_rolling_features(team_facts, [5])
        target = pd.DataFrame(
            [{"game_id": 101, "day": "2026-01-03", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20}]
        )
        snapshots = build_match_feature_snapshots(target, rolling)
        # Game 9 for team 10 is on the same target day and must not be used.
        self.assertNotEqual(int(snapshots.loc[0, "home_hist_game_id"]), 9)

    def test_train_predict_feature_parity(self):
        train = pd.DataFrame(
            [
                {"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20, "home_goals_for_roll_mean_5": 2.0, "away_goals_for_roll_mean_5": 1.5, "diff_goals_for_roll_mean_5": 0.5, "y_home_win": 1, "y_over_5_5": 0, "feature_set_version": "v1", "dataset_built_at": "x", "source_snapshot_id": "db"},
            ]
        )
        predict = pd.DataFrame(
            [
                {"game_id": 2, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 10, "away_team_id": 30, "home_goals_for_roll_mean_5": 1.9, "away_goals_for_roll_mean_5": 2.1, "diff_goals_for_roll_mean_5": -0.2, "feature_set_version": "v1", "dataset_built_at": "x", "low_history_confidence": 0, "quality_warnings": ""},
            ]
        )
        manifest = build_feature_manifest(train, feature_columns_from_df(train))
        assert_feature_parity(predict, manifest)

    def test_cold_start_policy(self):
        data = pd.DataFrame(
            [
                {"game_id": 1, "home_prior_games_count": 2, "away_prior_games_count": 10},
                {"game_id": 2, "home_prior_games_count": 7, "away_prior_games_count": 7},
            ]
        )
        train = apply_cold_start_policy(data, mode="train", min_prior_games=5, cold_start_policy_predict="allow_with_flag")
        self.assertEqual(train.dropped_rows, 1)
        self.assertEqual(train.data["game_id"].tolist(), [2])

        predict = apply_cold_start_policy(data, mode="predict", min_prior_games=5, cold_start_policy_predict="allow_with_flag")
        low_conf = dict(zip(predict.data["game_id"], predict.data["low_history_confidence"]))
        self.assertEqual(low_conf[1], 1)
        self.assertEqual(low_conf[2], 0)

    def test_features_hash_detects_cold_start_drift(self):
        train = pd.DataFrame(
            [
                {
                    "game_id": 1,
                    "day": "2026-01-01",
                    "season_id": 20252026,
                    "home_team_id": 10,
                    "away_team_id": 20,
                    "home_goals_for_roll_mean_5": 2.0,
                    "away_goals_for_roll_mean_5": 1.5,
                    "diff_goals_for_roll_mean_5": 0.5,
                    "y_home_win": 1,
                    "y_over_5_5": 0,
                    "feature_set_version": "v1",
                    "dataset_built_at": "x",
                    "source_snapshot_id": "db",
                },
            ]
        )
        manifest = build_feature_manifest(train, feature_columns_from_df(train))
        hash_allow = features_hash(
            feature_manifest=manifest,
            rolling_windows=[5, 10, 20],
            cold_start_policy="train:drop|predict:allow_with_flag",
            feature_set_version="v1",
        )
        hash_drop = features_hash(
            feature_manifest=manifest,
            rolling_windows=[5, 10, 20],
            cold_start_policy="train:drop|predict:drop",
            feature_set_version="v1",
        )
        self.assertNotEqual(hash_allow, hash_drop)

    def test_validate_fails_on_nan_keys(self):
        predict = pd.DataFrame(
            [
                {
                    "game_id": 2,
                    "day": "2026-01-02",
                    "season_id": 20252026,
                    "home_team_id": None,
                    "away_team_id": 30,
                    "home_goals_for_roll_mean_5": 1.9,
                    "away_goals_for_roll_mean_5": 2.1,
                    "diff_goals_for_roll_mean_5": -0.2,
                    "feature_set_version": "v1",
                    "dataset_built_at": "x",
                    "low_history_confidence": 0,
                    "quality_warnings": "",
                },
            ]
        )
        with self.assertRaisesRegex(ValueError, "NaN in key column: home_team_id"):
            validate_or_raise("predict", predict, feature_columns=feature_columns_from_df(predict))


if __name__ == "__main__":
    unittest.main()
