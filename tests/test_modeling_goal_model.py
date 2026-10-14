"""Goal model (Задача 67): settlement rule, tie rescaling, inputs, fit and the season-by-season check."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson

from modeling.goal_model import (
    MAX_GOALS,
    evaluate_goal_model,
    fit_for_upcoming,
    fit_goal_model,
    forecast,
    forecast_upcoming,
    p_over,
    prepare_games,
    score_grid,
    tracked_tie_rate,
)

SEASONS = (20212022, 20222023, 20232024, 20242025)


def _synthetic_games(seed: int = 0) -> pd.DataFrame:
    """Four seasons, 8 teams, 4 games a day for 40 days each; team 1 scores far more than the rest."""
    rng = np.random.default_rng(seed)
    rows = []
    game_id = 0
    for k, season in enumerate(SEASONS):
        start = pd.Timestamp(f"{2021 + k}-10-01")
        for day in range(40):
            teams = rng.permutation(8) + 1
            for home, away in zip(teams[::2], teams[1::2]):
                game_id += 1
                rows.append(
                    {
                        "game_id": game_id,
                        "day": (start + pd.Timedelta(days=day)).date().isoformat(),
                        "season_id": season,
                        "home_team_id": int(home),
                        "away_team_id": int(away),
                        "home_reg_goals": float(rng.poisson(5.0 if home == 1 else 3.0)),
                        "away_reg_goals": float(rng.poisson(5.0 if away == 1 else 2.8)),
                    }
                )
    return pd.DataFrame(rows)


def _one_cell_grid(home: int, away: int) -> np.ndarray:
    grid = np.zeros((1, MAX_GOALS + 1, MAX_GOALS + 1))
    grid[0, home, away] = 1.0
    return grid


class TestSettlementRule(unittest.TestCase):
    def test_tie_after_60_minutes_adds_exactly_one_goal(self) -> None:
        self.assertEqual(p_over(_one_cell_grid(2, 2), 5.5)[0], 0.0)  # 2:2 -> 5, under
        self.assertEqual(p_over(_one_cell_grid(3, 3), 5.5)[0], 1.0)  # 3:3 -> 7, over
        self.assertEqual(p_over(_one_cell_grid(3, 3), 6.5)[0], 1.0)
        self.assertEqual(p_over(_one_cell_grid(3, 2), 5.5)[0], 0.0)  # decided in regulation
        self.assertEqual(p_over(_one_cell_grid(4, 2), 5.5)[0], 1.0)
        self.assertEqual(p_over(_one_cell_grid(4, 2), 6.5)[0], 0.0)


class TestScoreGrid(unittest.TestCase):
    def test_mean_tie_probability_matches_the_target(self) -> None:
        lambdas = np.array([[3.2, 2.9], [2.5, 3.5], [4.0, 2.0]])
        grid = score_grid(lambdas, 0.23)
        np.testing.assert_allclose(grid.sum(axis=(1, 2)), 1.0)
        diagonal = grid[:, np.arange(MAX_GOALS + 1), np.arange(MAX_GOALS + 1)].sum(axis=1)
        self.assertAlmostEqual(diagonal.mean(), 0.23, places=9)
        # The closest game gets the most ties.
        self.assertEqual(int(np.argmax(diagonal)), 0)

    def test_independent_tie_rate_leaves_the_poisson_grid(self) -> None:
        lambdas = np.array([[3.0, 2.0]])
        goals = np.arange(MAX_GOALS + 1)
        independent = np.outer(poisson.pmf(goals, 3.0), poisson.pmf(goals, 2.0))
        independent /= independent.sum()
        grid = score_grid(lambdas, float(np.trace(independent)))
        np.testing.assert_allclose(grid[0], independent, atol=1e-9)


class TestInputs(unittest.TestCase):
    def test_back_to_back_tie_and_over_labels(self) -> None:
        games = pd.DataFrame(
            {
                "game_id": [3, 1, 2],
                "day": ["2025-10-03", "2025-10-01", "2025-10-02"],
                "season_id": [20252026] * 3,
                "home_team_id": [1, 1, 3],
                "away_team_id": [3, 2, 1],
                "home_reg_goals": [3.0, 2.0, 4.0],
                "away_reg_goals": [3.0, 2.0, 2.0],
            }
        )
        prepared = prepare_games(games)
        self.assertEqual(prepared["game_id"].tolist(), [1, 2, 3])
        # Team 1 plays on days 1, 2, 3; team 3 on days 2 and 3.
        self.assertEqual(prepared["home_b2b"].tolist(), [0.0, 0.0, 1.0])
        self.assertEqual(prepared["away_b2b"].tolist(), [0.0, 1.0, 1.0])
        self.assertEqual(prepared["reg_tie"].tolist(), [1.0, 0.0, 1.0])
        self.assertEqual(prepared["over"].tolist(), [0.0, 1.0, 1.0])  # 2:2 -> 5, 4:2 -> 6, 3:3 -> 7

    def test_missing_regulation_goals_fail(self) -> None:
        games = _synthetic_games().head(3)
        games.loc[1, "away_reg_goals"] = np.nan
        with self.assertRaises(ValueError):
            prepare_games(games)

    def test_tie_rate_uses_only_earlier_days_with_decay(self) -> None:
        games = prepare_games(
            pd.DataFrame(
                {
                    "game_id": [1, 2, 3, 4],
                    "day": ["2025-10-01", "2025-10-01", "2025-10-03", "2025-10-04"],
                    "season_id": [20252026] * 4,
                    "home_team_id": [1, 3, 1, 2],
                    "away_team_id": [2, 4, 3, 4],
                    "home_reg_goals": [2.0, 1.0, 3.0, 0.0],
                    "away_reg_goals": [2.0, 0.0, 3.0, 1.0],
                }
            )
        )
        rate = tracked_tie_rate(games, halflife_days=2.0)
        self.assertTrue(np.isnan(rate[:2]).all())
        self.assertAlmostEqual(rate[2], 0.5)  # day 1: one tie of two
        # Day 4: day 1 (weight 0.5**1.5, 1 of 2) and day 3 (weight 0.5**0.5, 1 of 1).
        w1, w3 = 0.5**1.5, 0.5**0.5
        self.assertAlmostEqual(rate[3], (w1 * 1 + w3 * 1) / (w1 * 2 + w3 * 1))


class TestFit(unittest.TestCase):
    def test_strong_attack_and_unseen_team(self) -> None:
        games = prepare_games(_synthetic_games())
        model = fit_goal_model(games, alpha=0.01)
        probe = pd.DataFrame(
            {
                "home_team_id": [1, 2, 99],
                "away_team_id": [2, 3, 2],
                "home_b2b": [0.0, 0.0, 0.0],
                "away_b2b": [0.0, 0.0, 0.0],
            }
        )
        lam = model.lambdas(probe)
        self.assertGreater(lam[0, 0], 4.0)  # team 1 at home scores ~5
        self.assertLess(lam[1, 0], 3.6)
        # A team without games is the league average: between the ordinary teams and team 1.
        self.assertGreater(lam[2, 0], lam[1, 0])
        self.assertLess(lam[2, 0], lam[0, 0])


class TestUpcoming(unittest.TestCase):
    """Задача 73: the published fit and the forecast of games without a result."""

    def test_upcoming_forecast_equals_the_forecast_of_the_same_games_once_played(self) -> None:
        # The last day as "upcoming" (goals dropped) must get the same P(over) as when it is
        # in the history: b2b and the tie rate look only at earlier days in both cases.
        games = _synthetic_games()
        last = games["day"] == games["day"].max()
        model = fit_goal_model(prepare_games(games[~last]), alpha=0.01)
        played_all = prepare_games(games)
        on_last = (played_all["day"] == pd.Timestamp(games["day"].max())).to_numpy()
        expected = forecast(model, played_all[on_last], tracked_tie_rate(played_all, 100)[on_last])

        upcoming = games.loc[last, ["game_id", "day", "home_team_id", "away_team_id"]]
        result = forecast_upcoming(model, games[~last], upcoming, 100)

        self.assertEqual(list(result["game_id"]), list(played_all.loc[on_last, "game_id"]))
        np.testing.assert_allclose(result["p_over"], expected["p_over"])
        self.assertGreater(played_all.loc[on_last, ["home_b2b", "away_b2b"]].to_numpy().sum(), 0)

    def test_no_upcoming_games_is_an_empty_forecast(self) -> None:
        games = _synthetic_games()
        model = fit_goal_model(prepare_games(games), alpha=0.01)
        result = forecast_upcoming(model, games, games.iloc[:0], 100)
        self.assertEqual(list(result.columns), ["game_id", "p_over"])
        self.assertTrue(result.empty)

    def test_published_fit_uses_every_game_and_a_ridge_from_the_grid(self) -> None:
        games = _synthetic_games()
        model, alpha = fit_for_upcoming(games, alphas=[0.01, 1.0], tie_halflife_days=100, epsilon=1e-15)
        self.assertIn(alpha, (0.01, 1.0))
        probe = prepare_games(games).tail(4)
        np.testing.assert_allclose(model.lambdas(probe), fit_goal_model(prepare_games(games), alpha).lambdas(probe))


class TestEvaluate(unittest.TestCase):
    def _evaluate(self, games: pd.DataFrame) -> dict:
        return evaluate_goal_model(
            games,
            test_seasons=[20232024, 20242025],
            alphas=[0.01, 1.0],
            tie_halflife_days=100,
            ece_bins=10,
            epsilon=1e-15,
            n_resamples=50,
            seed=0,
        )

    def test_report_and_gate_passes_on_a_real_team_signal(self) -> None:
        metrics = self._evaluate(_synthetic_games())
        self.assertEqual([s["season_id"] for s in metrics["seasons"]], [20232024, 20242025])
        self.assertEqual(metrics["pooled"]["n_test"], 320)
        ci = metrics["pooled"]["diff_ci"]["model_minus_constant"]
        self.assertLessEqual(ci["ci_low"], ci["point"])
        self.assertLess(ci["ci_high"], 0.0)  # team 1 scoring ~5 is a signal the constant lacks
        for season in metrics["seasons"]:
            self.assertLessEqual(abs(season["tie"]["predicted"] - season["tie"]["actual"]), 0.02)
        self.assertEqual(metrics["status"], "ok")
        # The constant is the over share of every game before the checked season.
        prepared = prepare_games(_synthetic_games())
        self.assertAlmostEqual(
            metrics["seasons"][1]["constant"]["p"], prepared.loc[prepared["season_id"] < 20242025, "over"].mean()
        )
        self.assertEqual(sum(row["count"] for row in metrics["lambda_sum_table"]), 320)

    def test_gate_fails_without_a_signal(self) -> None:
        games = _synthetic_games()
        rng = np.random.default_rng(0)
        games["home_reg_goals"] = rng.poisson(3.0, len(games)).astype(float)
        games["away_reg_goals"] = rng.poisson(2.8, len(games)).astype(float)
        metrics = self._evaluate(games)
        self.assertGreater(metrics["pooled"]["diff_ci"]["model_minus_constant"]["ci_high"], 0.0)
        self.assertEqual(metrics["status"], "failed_baseline_check")

    def test_forecasts_do_not_see_the_checked_games(self) -> None:
        games = _synthetic_games()
        last_day = games["day"].max()
        changed = games.copy()
        changed.loc[changed["day"] == last_day, ["home_reg_goals", "away_reg_goals"]] = 9.0
        before, after = self._evaluate(games)["seasons"], self._evaluate(changed)["seasons"]
        for a, b in zip(before, after):
            self.assertEqual(a["alpha"], b["alpha"])
            self.assertEqual(a["tie"]["predicted"], b["tie"]["predicted"])
            self.assertEqual(a["reg_total"]["predicted"], b["reg_total"]["predicted"])


class TestCli(unittest.TestCase):
    def test_fit_goals_writes_model_metadata_and_latest(self) -> None:
        import joblib

        from modeling.artifacts import resolve_latest_model_dir
        from modeling.cli import _handle_fit_goals, build_parser

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            _synthetic_games().to_csv(tmp / "games_train.csv", index=False)
            args = build_parser().parse_args(
                [
                    "fit-goals",
                    "--config",
                    "configs/modeling_smoke.yaml",
                    "--games",
                    str(tmp / "games_train.csv"),
                    "--artifacts-root",
                    str(tmp / "artifacts"),
                ]
            )
            self.assertEqual(_handle_fit_goals(args), 0)
            final = resolve_latest_model_dir(tmp / "artifacts", "over_5_5", "goals")
            metadata = json.loads((final / "metadata.json").read_text(encoding="utf-8"))
            model = joblib.load(final / "model.joblib")
        self.assertEqual(final.parent.name, metadata["run_id"])
        self.assertTrue(metadata["run_id"].startswith("over_5_5_goals_"))
        self.assertEqual(metadata["alpha"], 0.01)  # the smoke config's only ridge
        self.assertEqual(metadata["tie_halflife_days"], 100)
        self.assertEqual(metadata["n_games"], len(_synthetic_games()))
        self.assertEqual(model.teams, tuple(range(1, 9)))

    def test_evaluate_goals_writes_report_and_exit_code_follows_status(self) -> None:
        from modeling.cli import _handle_evaluate_goals, build_parser

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            _synthetic_games().to_csv(tmp / "games_train.csv", index=False)
            args = build_parser().parse_args(
                [
                    "evaluate-goals",
                    "--config",
                    "configs/modeling_smoke.yaml",
                    "--set",
                    "split.test_seasons=[20232024, 20242025]",
                    # The settings of TestEvaluate, so the run is the passing gate case.
                    "--set",
                    "goals.alpha=[0.01, 1.0]",
                    "--set",
                    "random_seed=0",
                    "--set",
                    "evaluation.bootstrap_samples=50",
                    "--games",
                    str(tmp / "games_train.csv"),
                    "--reports-root",
                    str(tmp / "reports"),
                ]
            )
            code = _handle_evaluate_goals(args)
            (report_dir,) = (tmp / "reports").iterdir()
            metrics = json.loads((report_dir / "metrics.json").read_text(encoding="utf-8"))
            summary = (report_dir / "summary.md").read_text(encoding="utf-8")
        self.assertTrue(report_dir.name.startswith("goals_"))
        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(code, 0)
        self.assertTrue(summary.startswith(f"status: {metrics['status']}\n"))
        self.assertIn("| 20242025 | 160 |", summary)


if __name__ == "__main__":
    unittest.main()
