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
    attach_pregame_elo,
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
from modeling.elo import compute_pregame_elo


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


class TestPregameElo(unittest.TestCase):
    """Задача 40 (Task 3): ``compute_pregame_elo``/``attach_pregame_elo``.

    Expected ratings below are computed independently from the formula in
    docs/modeling_dataset_builder.md, "Elo team-strength feature" (K=8,
    HFA=35, reg=1/3, MOV on, ot_s=1), not by calling the function under
    test, then rounded to 6 decimal places.
    """

    def _three_game_history(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                # team1 (home) beats team2 3-1
                {"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 2, "winner_id": 1, "home_goals": 3, "away_goals": 1},
                # team3 (new team, defaults to 1500) beats team1 (home) 2-0
                {"game_id": 2, "day": "2026-01-02", "season_id": 20252026, "home_team_id": 1, "away_team_id": 3, "winner_id": 3, "home_goals": 0, "away_goals": 2},
                # team2 (home) beats team3 1-0
                {"game_id": 3, "day": "2026-01-03", "season_id": 20252026, "home_team_id": 2, "away_team_id": 3, "winner_id": 2, "home_goals": 1, "away_goals": 0},
            ]
        )

    def test_hand_computed_pregame_ratings(self) -> None:
        per_game, final_ratings = compute_pregame_elo(self._three_game_history())
        rows = per_game.set_index("game_id")

        self.assertAlmostEqual(rows.loc[1, "home_elo"], 1500.0, places=6)
        self.assertAlmostEqual(rows.loc[1, "away_elo"], 1500.0, places=6)

        self.assertAlmostEqual(rows.loc[2, "home_elo"], 1503.891344, places=6)
        self.assertAlmostEqual(rows.loc[2, "away_elo"], 1500.0, places=6)

        self.assertAlmostEqual(rows.loc[3, "home_elo"], 1496.108656, places=6)
        self.assertAlmostEqual(rows.loc[3, "away_elo"], 1504.972210, places=6)

        # Elo is symmetric (zero-sum delta): with no new team since game 1,
        # the field mean stays exactly the start rating.
        self.assertAlmostEqual(sum(final_ratings.values()) / len(final_ratings), 1500.0, places=6)

    def test_asof_future_result_does_not_change_earlier_rating(self) -> None:
        """Changing game 3's outcome must not change games 1/2's pregame ratings."""
        baseline = self._three_game_history()
        changed = baseline.copy()
        changed.loc[changed["game_id"] == 3, ["winner_id", "home_goals", "away_goals"]] = [3, 0, 5]

        per_game_baseline, _ = compute_pregame_elo(baseline)
        per_game_changed, _ = compute_pregame_elo(changed)

        for game_id in (1, 2):
            base_row = per_game_baseline.set_index("game_id").loc[game_id]
            changed_row = per_game_changed.set_index("game_id").loc[game_id]
            self.assertAlmostEqual(base_row["home_elo"], changed_row["home_elo"], places=9)
            self.assertAlmostEqual(base_row["away_elo"], changed_row["away_elo"], places=9)

    def test_same_day_games_do_not_see_each_others_result(self) -> None:
        """Two same-day games sharing a team: the second must not reflect the first's update.

        Mirrors compute_team_rolling_features's intra_day_prev policy for rolling
        features: a same-day predecessor is masked out, not used, even though it
        sorts earlier by game_id.
        """
        hist = pd.DataFrame(
            [
                {"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 2, "winner_id": 1, "home_goals": 9, "away_goals": 0},
                {"game_id": 2, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 3, "winner_id": 3, "home_goals": 0, "away_goals": 1},
            ]
        )
        per_game, _ = compute_pregame_elo(hist)
        rows = per_game.set_index("game_id")
        # Game 2's home team (team 1) pregame rating must be the start rating,
        # not team 1's post-game-1 (blown-out win) rating.
        self.assertAlmostEqual(rows.loc[2, "home_elo"], 1500.0, places=6)

    def test_season_boundary_regresses_toward_mean(self) -> None:
        season_a = pd.DataFrame(
            [
                {"game_id": 1, "day": "2025-01-01", "season_id": 20242025, "home_team_id": 1, "away_team_id": 2, "winner_id": 1, "home_goals": 3, "away_goals": 1},
            ]
        )
        season_b = pd.concat(
            [
                season_a,
                pd.DataFrame(
                    [
                        {"game_id": 2, "day": "2025-10-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 2, "winner_id": 1, "home_goals": 2, "away_goals": 1},
                    ]
                ),
            ],
            ignore_index=True,
        )
        per_game, _ = compute_pregame_elo(season_b)
        rows = per_game.set_index("game_id")
        # 1/3 shrink toward the (exactly 1500, symmetric two-team) mean of the
        # ratings that came out of season A's only game.
        self.assertAlmostEqual(rows.loc[2, "home_elo"], 1502.594230, places=6)
        self.assertAlmostEqual(rows.loc[2, "away_elo"], 1497.405770, places=6)

    def test_predict_mode_uses_rating_after_last_played_game_no_extra_regression(self) -> None:
        """A future game in a season that has not been played yet gets the raw
        post-history rating (attach_pregame_elo's fallback), not a rating
        regressed for that not-yet-played season transition."""
        season_a = pd.DataFrame(
            [
                {"game_id": 1, "day": "2025-01-01", "season_id": 20242025, "home_team_id": 1, "away_team_id": 2, "winner_id": 1, "home_goals": 3, "away_goals": 1},
            ]
        )
        per_game, final_ratings = compute_pregame_elo(season_a)
        played_row = per_game.set_index("game_id").loc[1]

        future_target = pd.DataFrame(
            [
                # Unplayed game, next (unplayed) season — no rows of that season
                # exist in per_game to have triggered a regression event.
                {"game_id": 99, "day": "2025-10-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 2},
            ]
        )
        attached = attach_pregame_elo(future_target, per_game, final_ratings)
        self.assertAlmostEqual(float(attached.loc[0, "home_elo"]), final_ratings[1], places=9)
        self.assertAlmostEqual(float(attached.loc[0, "away_elo"]), final_ratings[2], places=9)
        # Sanity: this is the post-game-1 rating, not the pregame-game-1 default.
        self.assertNotAlmostEqual(float(attached.loc[0, "home_elo"]), 1500.0, places=3)
        self.assertNotAlmostEqual(float(attached.loc[0, "home_elo"]), float(played_row["home_elo"]), places=3)

    def test_unseen_team_defaults_to_start_rating(self) -> None:
        per_game, final_ratings = compute_pregame_elo(pd.DataFrame())
        self.assertTrue(per_game.empty)
        self.assertEqual(final_ratings, {})

        future_target = pd.DataFrame(
            [{"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 2}]
        )
        attached = attach_pregame_elo(future_target, per_game, final_ratings)
        self.assertAlmostEqual(float(attached.loc[0, "home_elo"]), 1500.0, places=9)
        self.assertAlmostEqual(float(attached.loc[0, "away_elo"]), 1500.0, places=9)

    def test_missing_result_fails_fast(self) -> None:
        hist = pd.DataFrame(
            [
                {"game_id": 1, "day": "2026-01-01", "season_id": 20252026, "home_team_id": 1, "away_team_id": 2, "winner_id": None, "home_goals": 3, "away_goals": 1},
            ]
        )
        with self.assertRaises(ValueError):
            compute_pregame_elo(hist)

    def test_diff_elo_built_by_assemble_without_sum_elo(self) -> None:
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
                    "home_elo": 1520.0,
                    "away_elo": 1480.0,
                }
            ]
        )
        data, report = assemble_dataset(
            "train",
            snapshots,
            min_prior_games=0,
            cold_start_policy_predict="allow_with_flag",
        )
        self.assertIn("diff_elo", data.columns)
        self.assertEqual(float(data.loc[0, "diff_elo"]), 40.0)
        self.assertNotIn("sum_elo", data.columns)
        self.assertNotIn("sum_elo", report["built_feature_columns"])


class TestLoadTargetGamesSource(unittest.TestCase):
    """Задача 22A: predict берёт цели из ``scheduled_games``, train — из ``games``."""

    def _query(self, mode: str, **kwargs: object) -> str:
        from pathlib import Path
        from unittest import mock

        from modeling.dataset_builder.base import DatasetBuildConfig, load_target_games

        config = DatasetBuildConfig(
            mode=mode, output_dir=Path("."), season_ids=[20262027], target_day_from="2026-10-01"
        )
        with mock.patch("modeling.dataset_builder.base.pd.read_sql_query") as read_sql:
            load_target_games(object(), config, **kwargs)  # type: ignore[arg-type]
        return read_sql.call_args.args[0]

    def test_predict_reads_scheduled_games_not_games(self):
        query = self._query("predict")
        self.assertIn("FROM scheduled_games g", query)
        self.assertNotIn("FROM games", query)
        self.assertNotIn("winner_id IS", query)
        self.assertIn("g.season_id IN (20262027)", query)
        self.assertIn("g.day >= '2026-10-01'", query)
        self.assertIn("ORDER BY g.day, g.game_id", query)

    def test_train_reads_played_games(self):
        query = self._query("train")
        self.assertIn("FROM games g", query)
        self.assertIn("g.winner_id IS NOT NULL", query)
        self.assertNotIn("scheduled_games", query)

    def test_decision_column_only_when_requested(self):
        # The feature table must not grow a ``decision`` column: only Elo / games_train.csv ask for it.
        self.assertNotIn("decision", self._query("train"))
        query = self._query("train", with_decision=True)
        self.assertIn("g.is_shootouts THEN 'SO'", query)
        self.assertIn("g.is_overtime THEN 'OT'", query)
        self.assertIn("AS decision", query)


class TestGamesTrainCsv(unittest.TestCase):
    """Задача 66: ``games_train.csv`` keeps every played game, cold-start rows included."""

    def _played(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "game_id": [1, 2, 3, 4],
                "day": pd.to_datetime(["2024-10-10", "2024-10-11", "2025-10-10", "2025-10-11"]),
                "season_id": [20242025, 20242025, 20252026, 20252026],
                "home_team_id": [1, 2, 1, 3],
                "away_team_id": [2, 3, 3, 1],
                "winner_id": [1, 3, 1, 1],
                "home_goals": [3.0, 1.0, 4.0, 2.0],
                "away_goals": [2.0, 2.0, 1.0, 3.0],
                "decision": ["REG", "OT", "SO", "REG"],
            }
        )

    def test_writes_all_games_with_decision(self):
        import tempfile
        from pathlib import Path

        from modeling.dataset_builder.base import DatasetBuildConfig, _write_games_train

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "games_train.csv"
            _write_games_train(self._played(), DatasetBuildConfig(mode="train", output_dir=Path(tmp)), path)
            written = pd.read_csv(path)

        self.assertEqual(
            list(written.columns),
            ["game_id", "day", "season_id", "home_team_id", "away_team_id", "home_goals", "away_goals", "decision"],
        )
        self.assertEqual(written["game_id"].tolist(), [1, 2, 3, 4])
        self.assertEqual(written["decision"].tolist(), ["REG", "OT", "SO", "REG"])
        self.assertNotIn("winner_id", written.columns)

    def test_limits_to_loaded_seasons(self):
        import tempfile
        from pathlib import Path

        from modeling.dataset_builder.base import DatasetBuildConfig, _write_games_train

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "games_train.csv"
            config = DatasetBuildConfig(mode="train", output_dir=Path(tmp), season_ids=[20252026])
            _write_games_train(self._played(), config, path)
            written = pd.read_csv(path)

        self.assertEqual(written["game_id"].tolist(), [3, 4])

    def test_round_trip_feeds_the_elo_history(self):
        import tempfile
        from pathlib import Path

        from modeling.dataset_builder.base import DatasetBuildConfig, _write_games_train
        from modeling.elo import EloHistory, EloParams

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "games_train.csv"
            _write_games_train(self._played(), DatasetBuildConfig(mode="train", output_dir=Path(tmp)), path)
            from_csv = EloHistory(pd.read_csv(path))
        from_builder_frame = EloHistory(self._played())

        params = EloParams(ot_win_weight=0.5)
        self.assertEqual(from_csv.diffs(params).tolist(), from_builder_frame.diffs(params).tolist())


if __name__ == "__main__":
    unittest.main()
