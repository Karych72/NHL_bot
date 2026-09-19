"""Dataset builder contract tests: anti-leakage, parity, and quality."""

from __future__ import annotations

import unittest

import pandas as pd

from modeling.dataset_builder.assemble import (
    _assert_train_labels_match_facts,
    apply_cold_start_policy,
    assemble_dataset,
)
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

    def test_three_rows_two_distinct_teams_dropped(self):
        hist = _history_df().copy()
        dup = hist[(hist["game_id"] == 3) & (hist["team_id"] == 20)].iloc[0]
        hist = pd.concat([hist, pd.DataFrame([dup])], ignore_index=True)
        self.assertEqual(int(hist[hist["game_id"] == 3].shape[0]), 3)
        team_facts, report = build_team_game_facts(hist)
        self.assertFalse((team_facts["game_id"] == 3).any())
        self.assertTrue(any(item["game_id"] == 3 for item in report["dropped_games"]))

    def test_double_header_rolling_ignores_same_day_prior_shift(self):
        def row(game_id, day, home, away, team, goals):
            return {
                "game_id": game_id,
                "day": day,
                "season_id": 20252026,
                "home_team_id": home,
                "away_team_id": away,
                "team_id": team,
                "goals": goals,
                "shots": 20,
                "pim": 5,
                "power_play_percentage": 20.0,
                "power_play_goals": 0,
                "power_play_opportunities": 3,
                "face_off_win_percentage": 50.0,
                "blocked": 10,
                "takeaways": 3,
                "giveaways": 5,
                "hits": 10,
            }

        hist = pd.DataFrame(
            [
                row(1, "2026-01-01", 1, 2, 1, 5),
                row(1, "2026-01-01", 1, 2, 2, 1),
                row(2, "2026-01-02", 1, 2, 1, 100),
                row(2, "2026-01-02", 1, 2, 2, 1),
                row(3, "2026-01-02", 1, 3, 1, 0),
                row(3, "2026-01-02", 1, 3, 3, 1),
            ]
        )
        team_facts, _ = build_team_game_facts(hist)
        rolling = compute_team_rolling_features(team_facts, [5])
        g3_team1 = rolling[(rolling["game_id"] == 3) & (rolling["team_id"] == 1)].iloc[0]
        self.assertLess(float(g3_team1["goals_for_roll_mean_5"]), 50.0)

    def test_zero_pp_opportunities_null_percentage_becomes_zero(self):
        hist = _history_df().copy()
        mask = (hist["game_id"] == 1) & (hist["team_id"] == 10)
        hist.loc[mask, "power_play_percentage"] = None
        hist.loc[mask, "power_play_opportunities"] = 0
        team_facts, _ = build_team_game_facts(hist)
        row10 = team_facts[(team_facts["game_id"] == 1) & (team_facts["team_id"] == 10)].iloc[0]
        row20 = team_facts[(team_facts["game_id"] == 1) & (team_facts["team_id"] == 20)].iloc[0]
        self.assertEqual(float(row10["power_play_percentage_for"]), 0.0)
        self.assertEqual(float(row20["power_play_percentage_against"]), 0.0)

    def test_pp_percentage_null_with_opportunities_stays_nan(self):
        # power_play_opportunities is left at the fixture default (5, not 0): a
        # NULL percentage here is "unknown" (missing source data), not "0
        # opportunities", and must not be silently zeroed — see
        # test_pp_percentage_null_with_opportunities_fails_fast_validation for the
        # downstream consequence (fail-fast, not a fabricated feature value).
        hist = _history_df().copy()
        hist.loc[(hist["game_id"] == 1) & (hist["team_id"] == 10), "power_play_percentage"] = None
        team_facts, _ = build_team_game_facts(hist)
        row10 = team_facts[(team_facts["game_id"] == 1) & (team_facts["team_id"] == 10)].iloc[0]
        self.assertTrue(pd.isna(row10["power_play_percentage_for"]))

    def test_pp_percentage_null_with_opportunities_fails_fast_validation(self):
        hist = _history_df().copy()
        hist.loc[(hist["game_id"] == 1) & (hist["team_id"] == 10), "power_play_percentage"] = None
        team_facts, _ = build_team_game_facts(hist)
        rolling = compute_team_rolling_features(team_facts, [5])
        # Target day 2026-01-02 (exact-match excluded) as-of-snapshots team 10's
        # most recent strictly-prior game, which is game 1 — the one with the
        # unknown (NULL, non-zero-opportunities) percentage.
        targets = pd.DataFrame(
            [{"game_id": 102, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20}]
        )
        snapshots = build_match_feature_snapshots(targets, rolling)
        snapshots["feature_set_version"] = "v1"
        snapshots["dataset_built_at"] = "2026-01-02T00:00:00Z"
        snapshots["low_history_confidence"] = 0
        snapshots["quality_warnings"] = ""
        with self.assertRaisesRegex(ValueError, "home_power_play_percentage_for"):
            validate_or_raise("predict", snapshots, feature_columns=feature_columns_from_df(snapshots))

    def test_power_play_percentage_above_100_is_clipped(self):
        hist = _history_df().copy()
        hist.loc[(hist["game_id"] == 1) & (hist["team_id"] == 10), "power_play_percentage"] = 200
        team_facts, _ = build_team_game_facts(hist)
        row10 = team_facts[(team_facts["game_id"] == 1) & (team_facts["team_id"] == 10)].iloc[0]
        row20 = team_facts[(team_facts["game_id"] == 1) & (team_facts["team_id"] == 20)].iloc[0]
        self.assertEqual(float(row10["power_play_percentage_for"]), 100.0)
        self.assertEqual(float(row20["power_play_percentage_against"]), 100.0)

    def test_rolling_and_snapshot_reset_at_season_boundary(self):
        def row(game_id, day, season_id, home, away, team, goals):
            return {
                "game_id": game_id,
                "day": day,
                "season_id": season_id,
                "home_team_id": home,
                "away_team_id": away,
                "team_id": team,
                "goals": goals,
                "shots": 20,
                "pim": 5,
                "power_play_percentage": 20.0,
                "power_play_goals": 0,
                "power_play_opportunities": 3,
                "face_off_win_percentage": 50.0,
                "blocked": 10,
                "takeaways": 3,
                "giveaways": 5,
                "hits": 10,
            }

        hist = pd.DataFrame(
            [
                # 2021/22 season: team 10 plays two games with an outlier goal
                # total that must not leak into the next season's rolling window
                # or as-of snapshot. Two games (not one) so team 10's most recent
                # 2021/22 game itself has a non-NaN goals_for_roll_mean_5 (mean of
                # game 1's 99) — otherwise the as-of assertion below would pass
                # even without the merge_asof season fix, since the single most
                # recent prior-season row would itself be NaN.
                row(1, "2021-10-01", 20212022, 10, 20, 10, 99),
                row(1, "2021-10-01", 20212022, 10, 20, 20, 1),
                row(4, "2021-10-03", 20212022, 10, 25, 10, 50),
                row(4, "2021-10-03", 20212022, 10, 25, 25, 0),
                # 2022/23 season: team 10's first game — no history this season yet.
                row(2, "2022-10-01", 20222023, 10, 30, 10, 1),
                row(2, "2022-10-01", 20222023, 10, 30, 30, 0),
                # 2022/23 season: team 10's second game.
                row(3, "2022-10-05", 20222023, 10, 40, 10, 2),
                row(3, "2022-10-05", 20222023, 10, 40, 40, 0),
            ]
        )
        team_facts, _ = build_team_game_facts(hist)
        rolling = compute_team_rolling_features(team_facts, [5])

        first_of_season = rolling[(rolling["game_id"] == 2) & (rolling["team_id"] == 10)].iloc[0]
        self.assertEqual(int(first_of_season["prior_games_count"]), 0)
        self.assertTrue(pd.isna(first_of_season["goals_for_roll_mean_5"]))

        second_of_season = rolling[(rolling["game_id"] == 3) & (rolling["team_id"] == 10)].iloc[0]
        self.assertEqual(float(second_of_season["goals_for_roll_mean_5"]), 1.0)

        # Same check through the as-of snapshot path (build_match_feature_snapshots /
        # _snapshot_side): the merge_asof leak fixed independently of the rolling fix.
        targets = pd.DataFrame(
            [
                {"game_id": 2, "day": "2022-10-01", "season_id": 20222023, "home_team_id": 10, "away_team_id": 30},
            ]
        )
        snapshots = build_match_feature_snapshots(targets, rolling)
        self.assertTrue(pd.isna(snapshots.loc[0, "home_hist_game_id"]))
        self.assertTrue(pd.isna(snapshots.loc[0, "home_goals_for_roll_mean_5"]))

    def test_assemble_excludes_goals_target_wide_features(self):
        snapshots = pd.DataFrame(
            [
                {
                    "game_id": 1,
                    "day": "2026-01-01",
                    "season_id": 20252026,
                    "home_team_id": 10,
                    "away_team_id": 20,
                    "winner_id": 10,
                    "home_goals_target": 4,
                    "away_goals_target": 2,
                    "home_prior_games_count": 8,
                    "away_prior_games_count": 8,
                    "home_foo": 1.0,
                    "away_foo": 2.0,
                }
            ]
        )
        data, _ = assemble_dataset(
            "train",
            snapshots,
            min_prior_games=0,
            cold_start_policy_predict="allow_with_flag",
        )
        self.assertIn("diff_foo", data.columns)
        self.assertNotIn("diff_goals_target", data.columns)
        self.assertNotIn("sum_goals_target", data.columns)

    def test_validate_rejects_forbidden_feature_names(self):
        df = pd.DataFrame(
            [
                {
                    "game_id": 1,
                    "day": "2026-01-01",
                    "season_id": 20252026,
                    "home_team_id": 10,
                    "away_team_id": 20,
                    "y_home_win": 1,
                    "y_over_5_5": 0,
                    "diff_goals_target": 0.0,
                }
            ]
        )
        with self.assertRaises(ValueError) as ctx:
            validate_or_raise("train", df, feature_columns=[])
        self.assertIn("forbidden", str(ctx.exception).lower())

    def test_train_label_assert_detects_mismatch(self):
        df = pd.DataFrame(
            [
                {
                    "winner_id": 10,
                    "home_team_id": 10,
                    "y_home_win": 0,
                    "y_over_5_5": 0,
                    "home_goals_target": 2.0,
                    "away_goals_target": 2.0,
                }
            ]
        )
        with self.assertRaises(ValueError):
            _assert_train_labels_match_facts(df)

    def test_no_merge_suffix_columns_in_snapshots(self):
        team_facts, _ = build_team_game_facts(_history_df())
        rolling = compute_team_rolling_features(team_facts, [5])
        targets = pd.DataFrame(
            [
                {
                    "game_id": 100,
                    "day": "2026-01-04",
                    "season_id": 20252026,
                    "home_team_id": 10,
                    "away_team_id": 20,
                }
            ]
        )
        snapshots = build_match_feature_snapshots(targets, rolling)
        for col in snapshots.columns:
            self.assertFalse(col.endswith("_x"), col)
            self.assertFalse(col.endswith("_y"), col)

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

    def test_snapshot_audit_columns_excluded_from_features(self):
        """Задача 15: hist_*/opponent_team_id/home_team_id/away_team_id snapshot
        columns are audit-only (see feature_schema.AUDIT_COLUMNS) and must never
        leak into the feature set, even though build_match_feature_snapshots
        still emits them for validate.py's anti-leakage checks."""
        team_facts, _ = build_team_game_facts(_history_df())
        rolling = compute_team_rolling_features(team_facts, [5])
        targets = pd.DataFrame(
            [{"game_id": 100, "day": "2026-01-04", "season_id": 20252026, "home_team_id": 10, "away_team_id": 20}]
        )
        snapshots = build_match_feature_snapshots(targets, rolling)
        audit_cols_present = [
            "home_hist_day",
            "home_hist_game_id",
            "home_opponent_team_id",
            "home_home_team_id",
            "home_away_team_id",
            "away_hist_day",
            "away_hist_game_id",
            "away_opponent_team_id",
            "away_home_team_id",
            "away_away_team_id",
        ]
        for col in audit_cols_present:
            self.assertIn(col, snapshots.columns, f"fixture assumption broken: {col} missing from snapshot")
        features = feature_columns_from_df(snapshots)
        for col in audit_cols_present:
            self.assertNotIn(col, features, f"{col} leaked into feature_columns_from_df")


class TestAlignPredictToManifest(unittest.TestCase):
    """Задача 15: `_align_predict_to_manifest` casts predict feature dtypes to
    the train manifest for *any* row count, not only the empty-predict case —
    see module docstring for why train/predict can legitimately disagree on a
    snapshot column's pandas dtype."""

    def test_casts_existing_column_dtype_for_non_empty_frame(self):
        from modeling.dataset_builder.base import _align_predict_to_manifest

        assembled = pd.DataFrame({"game_id": [1, 2], "home_is_home": [1, 0]})
        self.assertEqual(str(assembled["home_is_home"].dtype), "int64")
        manifest = [{"name": "home_is_home", "dtype": "float64", "position": "0"}]

        out = _align_predict_to_manifest(assembled, manifest)

        self.assertEqual(str(out["home_is_home"].dtype), "float64")
        self.assertEqual(len(out), 2)

    def test_does_not_fabricate_missing_column_for_non_empty_frame(self):
        from modeling.dataset_builder.base import _align_predict_to_manifest

        assembled = pd.DataFrame({"game_id": [1, 2], "f_a": [1.0, 2.0]})
        manifest = [
            {"name": "f_a", "dtype": "float64", "position": "0"},
            {"name": "f_missing", "dtype": "float64", "position": "1"},
        ]

        out = _align_predict_to_manifest(assembled, manifest)

        self.assertNotIn("f_missing", out.columns)
        with self.assertRaises(ValueError):
            assert_feature_parity(out, manifest)


if __name__ == "__main__":
    unittest.main()
