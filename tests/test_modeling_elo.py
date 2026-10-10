"""Tests for modeling.elo: the feature core, the parameter grid and the leakage-free benchmark."""

from __future__ import annotations

import random
import unittest

import numpy as np
import pandas as pd

from modeling.elo import (
    EloBenchmark,
    EloHistory,
    EloParams,
    _fit_platt,
    compute_pregame_elo,
    elo_season_predictions,
    expand_elo_grid,
    fit_elo_benchmark,
)


def _game(game_id: int, day: str, home: int, away: int, hg: int, ag: int, *, season: int = 20232024, decision: str = "REG") -> dict:
    return {
        "game_id": game_id,
        "day": day,
        "season_id": season,
        "home_team_id": home,
        "away_team_id": away,
        "home_goals": hg,
        "away_goals": ag,
        "decision": decision,
    }


def _legacy_feature_history() -> pd.DataFrame:
    """80 games over two seasons, built with ``winner_id`` like the dataset builder's input."""
    rng = random.Random(7)
    rows = []
    game_id = 1
    days = list(pd.date_range("2023-10-10", periods=30, freq="D")) + list(
        pd.date_range("2024-10-10", periods=10, freq="D")
    )
    for day_index, day in enumerate(days):
        season = 20232024 if day_index < 30 else 20242025
        teams = rng.sample(range(1, 9), 4)
        for home, away in ((teams[0], teams[1]), (teams[2], teams[3])):
            hg, ag = rng.randint(0, 6), rng.randint(0, 6)
            if hg == ag:
                hg += 1
            rows.append(
                {
                    "game_id": game_id,
                    "day": str(day.date()),
                    "season_id": season,
                    "home_team_id": home,
                    "away_team_id": away,
                    "winner_id": home if hg > ag else away,
                    "home_goals": hg,
                    "away_goals": ag,
                }
            )
            game_id += 1
    return pd.DataFrame(rows)


def _league(*, n_seasons: int = 4, days: int = 120, seed: int = 0) -> pd.DataFrame:
    """Four seasons of 8 teams with real strength differences; home side has an edge."""
    rng = np.random.default_rng(seed)
    strength = rng.normal(0.0, 0.35, size=9)
    rows = []
    game_id = 1
    for season_index in range(n_seasons):
        season = (2020 + season_index) * 10000 + 2021 + season_index
        for day in pd.date_range(f"{2020 + season_index}-10-10", periods=days, freq="D"):
            teams = rng.permutation(np.arange(1, 9))
            for home, away in ((teams[0], teams[1]), (teams[2], teams[3]), (teams[4], teams[5])):
                p_home = 1.0 / (1.0 + np.exp(-(0.15 + strength[home] - strength[away])))
                home_wins = rng.random() < p_home
                rows.append(
                    _game(game_id, str(day.date()), int(home), int(away), 4 if home_wins else 1, 1 if home_wins else 4, season=season)
                )
                game_id += 1
    return pd.DataFrame(rows)


class TestFeatureCoreUnchanged(unittest.TestCase):
    """Default ``EloParams`` must reproduce the pre-Задача 66 ``diff_elo`` feature exactly.

    Expected numbers were produced by the old ``features.compute_pregame_elo`` (K=8, HFA=35,
    regression 1/3, MOV on, OT = full win) on ``_legacy_feature_history`` before the move.
    """

    def test_default_params_match_old_feature_values(self) -> None:
        per_game, final = compute_pregame_elo(_legacy_feature_history())

        self.assertEqual(len(per_game), 80)
        self.assertAlmostEqual(per_game["home_elo"].sum(), 119952.163843, places=5)
        self.assertAlmostEqual(per_game["away_elo"].sum(), 119822.845717, places=5)
        np.testing.assert_allclose(
            per_game["home_elo"].tail(3), [1475.503510273, 1491.356457297, 1490.732485019], atol=1e-8
        )
        np.testing.assert_allclose(
            per_game["away_elo"].tail(3), [1500.70973554, 1512.229005895, 1505.357862461], atol=1e-8
        )
        expected_final = {
            1: 1511.282724093,
            2: 1505.642226485,
            3: 1518.468717031,
            4: 1469.776567879,
            5: 1506.436677934,
            6: 1493.999397086,
            7: 1509.586066106,
            8: 1484.807623387,
        }
        self.assertEqual(sorted(final), sorted(expected_final))
        for team, rating in expected_final.items():
            self.assertAlmostEqual(final[team], rating, places=7)

    def test_decision_column_with_default_weight_changes_nothing(self) -> None:
        games = _legacy_feature_history()
        plain, _ = compute_pregame_elo(games)
        games["decision"] = ["OT" if i % 3 == 0 else "SO" if i % 7 == 0 else "REG" for i in range(len(games))]
        with_decision, _ = compute_pregame_elo(games)

        pd.testing.assert_frame_equal(plain, with_decision)


class TestEloMechanics(unittest.TestCase):
    NO_HFA_NO_MOV = dict(home_advantage=0.0, mov=False, season_regression=0.0)

    def _after_first_game(self, decision: str, params: EloParams) -> float:
        games = pd.DataFrame(
            [
                _game(1, "2024-01-01", 1, 2, 3, 2, decision=decision),
                _game(2, "2024-01-02", 1, 3, 1, 0),
            ]
        )
        per_game, _ = compute_pregame_elo(games, params)
        return float(per_game.set_index("game_id").loc[2, "home_elo"])

    def test_full_weight_overtime_win_counts_like_regulation(self) -> None:
        params = EloParams(k=10.0, ot_win_weight=1.0, **self.NO_HFA_NO_MOV)

        self.assertAlmostEqual(self._after_first_game("REG", params), 1505.0)
        self.assertAlmostEqual(self._after_first_game("OT", params), 1505.0)
        self.assertAlmostEqual(self._after_first_game("SO", params), 1505.0)

    def test_half_weight_overtime_win_is_a_draw_for_the_update(self) -> None:
        params = EloParams(k=10.0, ot_win_weight=0.5, **self.NO_HFA_NO_MOV)

        self.assertAlmostEqual(self._after_first_game("OT", params), 1500.0)
        self.assertAlmostEqual(self._after_first_game("SO", params), 1500.0)
        self.assertAlmostEqual(self._after_first_game("REG", params), 1505.0)

    def test_intermediate_weight_credits_that_outcome(self) -> None:
        params = EloParams(k=10.0, ot_win_weight=0.75, **self.NO_HFA_NO_MOV)

        # expected 0.5, outcome 0.75 -> +10 * 0.25
        self.assertAlmostEqual(self._after_first_game("OT", params), 1502.5)

    def test_mov_switch_changes_update_size(self) -> None:
        games = pd.DataFrame(
            [_game(1, "2024-01-01", 1, 2, 6, 0), _game(2, "2024-01-02", 1, 3, 1, 0)]
        )
        on, _ = compute_pregame_elo(games, EloParams(home_advantage=0.0, mov=True))
        off, _ = compute_pregame_elo(games, EloParams(home_advantage=0.0, mov=False))

        self.assertAlmostEqual(off.set_index("game_id").loc[2, "home_elo"], 1504.0)
        self.assertGreater(on.set_index("game_id").loc[2, "home_elo"], 1504.0)

    def test_ratings_are_frozen_for_the_calendar_day(self) -> None:
        games = pd.DataFrame(
            [
                _game(1, "2024-01-01", 1, 2, 3, 0),
                _game(2, "2024-01-01", 1, 3, 3, 0),
                _game(3, "2024-01-02", 1, 4, 3, 0),
            ]
        )
        per_game, _ = compute_pregame_elo(games)
        rows = per_game.set_index("game_id")

        self.assertEqual(rows.loc[1, "home_elo"], 1500.0)
        self.assertEqual(rows.loc[2, "home_elo"], 1500.0)
        self.assertGreater(rows.loc[3, "home_elo"], 1500.0)

    def test_season_change_shrinks_ratings_toward_the_mean(self) -> None:
        games = pd.DataFrame(
            [
                _game(1, "2024-01-01", 1, 2, 3, 0, season=20232024),
                _game(2, "2024-10-10", 1, 2, 3, 0, season=20242025),
            ]
        )
        base = dict(home_advantage=0.0, mov=False)
        kept, _ = compute_pregame_elo(games, EloParams(season_regression=0.0, **base))
        halved, _ = compute_pregame_elo(games, EloParams(season_regression=0.5, **base))

        gap_kept = kept["home_elo"].iloc[1] - kept["away_elo"].iloc[1]
        gap_halved = halved["home_elo"].iloc[1] - halved["away_elo"].iloc[1]
        self.assertAlmostEqual(gap_halved, gap_kept / 2.0)

    def test_params_validate_ot_weight(self) -> None:
        for bad in (0.49, 1.01):
            with self.assertRaises(ValueError):
                EloParams(ot_win_weight=bad)

    def test_missing_result_fails_loudly(self) -> None:
        games = pd.DataFrame([_game(1, "2024-01-01", 1, 2, 3, 0)])
        games.loc[0, "home_goals"] = np.nan
        with self.assertRaises(ValueError):
            EloHistory(games)

    def test_level_game_fails_loudly(self) -> None:
        with self.assertRaises(ValueError):
            EloHistory(pd.DataFrame([_game(1, "2024-01-01", 1, 2, 2, 2)]))


class TestGrid(unittest.TestCase):
    def test_expand_grid_is_the_cartesian_product(self) -> None:
        grid = expand_elo_grid(
            {
                "k": [4, 8],
                "home_advantage": [20, 35, 50],
                "season_regression": [0.2],
                "mov": [True, False],
                "ot_win_weight": [1.0, 0.5],
            }
        )

        self.assertEqual(len(grid), 2 * 3 * 1 * 2 * 2)
        self.assertEqual(len(set(grid)), len(grid))
        self.assertIn(EloParams(k=8, home_advantage=50, season_regression=0.2, mov=False, ot_win_weight=0.5), grid)


class TestPlattFit(unittest.TestCase):
    def test_recovers_known_curve(self) -> None:
        rng = np.random.default_rng(1)
        d = rng.normal(0.0, 60.0, size=40000)
        y = (rng.random(d.size) < 1.0 / (1.0 + np.exp(-(0.2 + 0.006 * d)))).astype(float)

        a, b = _fit_platt(d, y)

        self.assertAlmostEqual(a, 0.2, delta=0.04)
        self.assertAlmostEqual(b, 0.006, delta=0.0005)


class TestBenchmark(unittest.TestCase):
    GRID = expand_elo_grid(
        {"k": [4, 12], "home_advantage": [20, 50], "season_regression": [0.3], "mov": [True], "ot_win_weight": [1.0]}
    )

    def setUp(self) -> None:
        self.games = _league()
        self.seasons = sorted(self.games["season_id"].unique())

    def test_fit_ignores_the_checked_season_and_later_ones(self) -> None:
        test_season = self.seasons[2]
        baseline = fit_elo_benchmark(EloHistory(self.games), test_season, self.GRID)

        tampered = self.games.copy()
        later = tampered["season_id"] >= test_season
        tampered.loc[later, ["home_goals", "away_goals"]] = tampered.loc[later, ["away_goals", "home_goals"]].to_numpy()
        changed = fit_elo_benchmark(EloHistory(tampered), test_season, self.GRID)

        self.assertEqual(changed.params, baseline.params)
        self.assertAlmostEqual(changed.a, baseline.a, places=12)
        self.assertAlmostEqual(changed.b, baseline.b, places=12)
        self.assertEqual(changed.n_fit_games, baseline.n_fit_games)

    def test_fit_excludes_the_warm_up_season(self) -> None:
        history = EloHistory(self.games)
        first = int((self.games["season_id"] == self.seasons[0]).sum())
        second = int((self.games["season_id"] == self.seasons[1]).sum())

        benchmark = fit_elo_benchmark(history, self.seasons[2], self.GRID)

        self.assertEqual(benchmark.n_fit_games, second)
        benchmark3 = fit_elo_benchmark(history, self.seasons[3], self.GRID)
        self.assertEqual(benchmark3.n_fit_games, second + int((self.games["season_id"] == self.seasons[2]).sum()))
        self.assertGreater(first, 0)

    def test_only_warm_up_season_before_checked_one_fails(self) -> None:
        history = EloHistory(self.games)
        for season in (self.seasons[0], self.seasons[1]):
            with self.subTest(season=season):
                with self.assertRaises(ValueError):
                    fit_elo_benchmark(history, season, self.GRID)

    def test_empty_grid_fails(self) -> None:
        with self.assertRaises(ValueError):
            fit_elo_benchmark(EloHistory(self.games), self.seasons[2], [])

    def test_chosen_point_is_the_best_fit_loss(self) -> None:
        history = EloHistory(self.games)
        test_season = self.seasons[3]
        best = fit_elo_benchmark(history, test_season, self.GRID)

        for params in self.GRID:
            single = fit_elo_benchmark(history, test_season, [params])
            self.assertLessEqual(best.fit_log_loss, single.fit_log_loss + 1e-12)
        self.assertIn(best.params, self.GRID)

    def test_predictions_cover_the_whole_season_with_both_curves(self) -> None:
        history = EloHistory(self.games)
        test_season = self.seasons[3]
        benchmark = fit_elo_benchmark(history, test_season, self.GRID)

        preds = elo_season_predictions(history, benchmark)

        season_games = self.games[self.games["season_id"] == test_season]
        self.assertEqual(sorted(preds["game_id"]), sorted(season_games["game_id"]))
        d = history.diffs(benchmark.params)[history.season_id == test_season]
        np.testing.assert_allclose(preds["p_fitted"], 1.0 / (1.0 + np.exp(-(benchmark.a + benchmark.b * d))))
        np.testing.assert_allclose(
            preds["p_raw"], 1.0 / (1.0 + 10.0 ** (-(d + benchmark.params.home_advantage) / 400.0))
        )
        by_id = season_games.set_index("game_id")
        expected_home_win = (by_id["home_goals"] > by_id["away_goals"]).astype(float)
        np.testing.assert_array_equal(
            preds.set_index("game_id")["home_win"].sort_index(), expected_home_win.sort_index()
        )

    def test_forecast_for_a_game_ignores_later_games(self) -> None:
        history = EloHistory(self.games)
        test_season = self.seasons[3]
        benchmark = fit_elo_benchmark(history, test_season, self.GRID)
        before = elo_season_predictions(history, benchmark).set_index("game_id")

        truncated = self.games[self.games["game_id"] <= self.games["game_id"].quantile(0.9)]
        cut_history = EloHistory(truncated)
        after = elo_season_predictions(cut_history, benchmark).set_index("game_id")

        np.testing.assert_allclose(after["p_fitted"], before.loc[after.index, "p_fitted"])

    def test_unknown_season_fails(self) -> None:
        benchmark = EloBenchmark(29992000, EloParams(), 0.0, 0.0, 0.69, 10)
        with self.assertRaises(ValueError):
            elo_season_predictions(EloHistory(self.games), benchmark)

    def test_to_dict_has_the_report_fields(self) -> None:
        benchmark = fit_elo_benchmark(EloHistory(self.games), self.seasons[2], self.GRID)
        payload = benchmark.to_dict()

        for key in ("k", "home_advantage", "season_regression", "mov", "ot_win_weight", "a", "b"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
