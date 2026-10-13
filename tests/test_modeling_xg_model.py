"""Fenwick-xG (Задача 69): side normalisation, exclusions, rebound and strength features, fit, check, CLI."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from modeling.xg_model import evaluate_xg_model, fit_xg, predict_xg, prepare_shots

HOME, AWAY = 10, 20


def _attempt(**overrides: object) -> dict[str, object]:
    """One home shot-on-goal at 5v5 from (70, 0) in game 1, period 1; ``overrides`` change any field."""
    row: dict[str, object] = {
        "game_id": 1, "event_id": 0, "sort_order": 0, "period": 1, "period_type": "REG",
        "time_in_period": "05:00", "team_id": HOME, "event_type": "shot-on-goal",
        "x_coord": 70, "y_coord": 0, "zone_code": "O", "shot_type": "wrist",
        "situation_code": "1551", "home_team_defending_side": "left", "shooter_id": 1,
        "goalie_in_net_id": 2, "season_id": 20232024, "day": "2023-10-10",
        "home_team_id": HOME, "away_team_id": AWAY,
    }
    row.update(overrides)
    return row


def _frame(*rows: dict[str, object]) -> pd.DataFrame:
    """Attempts in the given order: ``event_id`` / ``sort_order`` follow it unless set."""
    return pd.DataFrame([{"event_id": i, "sort_order": i, **r} for i, r in enumerate(rows)])


class TestNormalisation(unittest.TestCase):
    def test_every_shooter_attacks_plus_x_whatever_the_side(self) -> None:
        # Raw coordinates of a shot from 19 ft in front of the attacked net, 10 ft to one side.
        frame = _frame(
            _attempt(game_id=1, team_id=HOME, home_team_defending_side="left", x_coord=70, y_coord=10),
            _attempt(game_id=2, team_id=HOME, home_team_defending_side="right", x_coord=-70, y_coord=-10),
            _attempt(game_id=3, team_id=AWAY, home_team_defending_side="left", x_coord=-70, y_coord=-10),
            _attempt(game_id=4, team_id=AWAY, home_team_defending_side="right", x_coord=70, y_coord=10),
        )
        prepared, _ = prepare_shots(frame)
        self.assertEqual(prepared["x_norm"].tolist(), [70.0, 70.0, 70.0, 70.0])
        self.assertEqual(prepared["y_norm"].tolist(), [10.0, 10.0, 10.0, 10.0])
        np.testing.assert_allclose(prepared["distance"], np.hypot(19.0, 10.0))
        np.testing.assert_allclose(prepared["angle"], np.degrees(np.arctan2(10.0, 19.0)))

    def test_angle_from_the_side_and_behind_the_net(self) -> None:
        prepared, _ = prepare_shots(
            _frame(_attempt(game_id=1, x_coord=79, y_coord=-10), _attempt(game_id=2, x_coord=94, y_coord=0))
        )
        np.testing.assert_allclose(prepared["angle"], [45.0, 180.0])
        np.testing.assert_allclose(prepared["distance"], [np.hypot(10.0, 10.0), 5.0])


class TestExclusions(unittest.TestCase):
    def test_empty_net_no_coordinates_and_blocked_shots_are_counted_out(self) -> None:
        frame = _frame(
            _attempt(game_id=1),  # kept
            _attempt(game_id=2, team_id=HOME, situation_code="0651"),  # away net empty: excluded
            _attempt(game_id=3, team_id=AWAY, situation_code="1560"),  # home net empty: excluded
            _attempt(game_id=4, team_id=HOME, situation_code="1560"),  # own net empty: kept, "other"
            _attempt(game_id=5, x_coord=None, y_coord=None),  # no coordinates: excluded
            _attempt(game_id=6, event_type="blocked-shot", shot_type=None),  # not Fenwick
            _attempt(game_id=7, event_type="goal", shot_type=None),  # kept, shot type unknown
        )
        prepared, counts = prepare_shots(frame)
        self.assertEqual(
            counts,
            {"attempts": 7, "fenwick": 6, "excluded_empty_net": 2, "excluded_no_coordinates": 1, "kept": 3},
        )
        self.assertEqual(prepared["game_id"].tolist(), [1, 4, 7])
        self.assertEqual(prepared["strength"].tolist(), ["even", "other", "even"])
        self.assertEqual(prepared["shot_type"].tolist(), ["wrist", "wrist", "unknown"])
        self.assertEqual(prepared["is_goal"].tolist(), [0.0, 0.0, 1.0])

    def test_numeric_situation_code_fails(self) -> None:
        frame = _frame(_attempt())
        frame["situation_code"] = 1551
        with self.assertRaisesRegex(ValueError, "four digits"):
            prepare_shots(frame)

    def test_missing_required_field_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "home_team_defending_side"):
            prepare_shots(_frame(_attempt(home_team_defending_side=None)))


class TestFeatures(unittest.TestCase):
    def test_rebound_is_a_same_team_attempt_within_three_seconds_of_the_period(self) -> None:
        frame = _frame(
            _attempt(time_in_period="05:00", event_type="blocked-shot", shot_type=None),
            _attempt(time_in_period="05:03"),  # 3 s after own blocked shot: rebound
            _attempt(time_in_period="05:07"),  # 4 s: not
            _attempt(time_in_period="05:08", team_id=AWAY),  # other team: not
            _attempt(time_in_period="05:09"),  # previous attempt is the opponent's: not
            _attempt(time_in_period="00:01", period=2),  # new period: not
            _attempt(time_in_period="00:00", period=2),  # clock before the previous attempt: not
        )
        prepared, _ = prepare_shots(frame)
        self.assertEqual(prepared["rebound"].tolist(), [1.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    def test_rebound_follows_sort_order_not_row_order(self) -> None:
        frame = _frame(_attempt(time_in_period="05:02"), _attempt(time_in_period="05:00"))
        frame["sort_order"] = [2, 1]
        prepared, _ = prepare_shots(frame)
        self.assertEqual(prepared["time_in_period"].tolist(), ["05:00", "05:02"])
        self.assertEqual(prepared["rebound"].tolist(), [0.0, 1.0])

    def test_strength_from_the_shooters_side(self) -> None:
        frame = _frame(
            _attempt(game_id=1, team_id=HOME, situation_code="1451"),  # home 5 v away 4
            _attempt(game_id=2, team_id=AWAY, situation_code="1451"),
            _attempt(game_id=3, team_id=AWAY, situation_code="1441"),
        )
        prepared, _ = prepare_shots(frame)
        self.assertEqual(prepared["strength"].tolist(), ["pp", "sh", "even"])


def _synthetic_shots(seasons: tuple[int, ...], per_season: int = 3000, seed: int = 0) -> pd.DataFrame:
    """One shot per game; P(goal) = sigmoid(0.5 − 0.08 · distance), sides and shooters random."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in seasons:
        for i in range(per_season):
            x, y = rng.uniform(30, 88), rng.uniform(-35, 35)
            p = 1.0 / (1.0 + np.exp(-(0.5 - 0.08 * np.hypot(89 - x, y))))
            home, left = bool(rng.integers(2)), bool(rng.integers(2))
            sign = 1 if home == left else -1
            rows.append(
                _attempt(
                    game_id=season * 10_000 + i,
                    season_id=season,
                    team_id=HOME if home else AWAY,
                    home_team_defending_side="left" if left else "right",
                    x_coord=int(round(sign * x)),
                    y_coord=int(round(sign * y)),
                    shot_type=str(rng.choice(["wrist", "slap", "snap"])),
                    event_type="goal" if rng.random() < p else "shot-on-goal",
                )
            )
    return _frame(*rows)


class TestFitAndCheck(unittest.TestCase):
    def test_xg_falls_with_distance(self) -> None:
        prepared, _ = prepare_shots(_synthetic_shots((20212022,)))
        model = fit_xg(prepared)
        grid = prepare_shots(_frame(*[_attempt(game_id=i, x_coord=89 - d) for i, d in enumerate(range(5, 60, 5))]))[0]
        xg = predict_xg(model, grid)
        self.assertTrue(np.all(np.diff(xg) < 0), msg=str(xg))
        self.assertGreater(xg[0], 0.3)
        self.assertLess(xg[-1], 0.05)

    def test_season_by_season_check(self) -> None:
        seasons = (20212022, 20222023, 20232024)
        metrics = evaluate_xg_model(_synthetic_shots(seasons), test_seasons=[20222023, 20232024], epsilon=1e-6)
        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["counts"]["kept"], 9000)
        self.assertEqual(metrics["normalization_check"]["zone_code_agreement"], 1.0)
        self.assertEqual(metrics["normalization_check"]["goals_beyond_blue_line"], 1.0)
        first, second = metrics["seasons"]
        self.assertEqual((first["n_train"], first["n_test"]), (3000, 3000))
        self.assertEqual((second["n_train"], second["n_test"]), (6000, 3000))
        for s in metrics["seasons"]:
            self.assertGreater(s["auc"], 0.7)
            self.assertLess(s["log_loss"], s["constant"]["log_loss"])
            self.assertAlmostEqual(s["xg_sum"] / s["goals"], 1.0, delta=0.1)
            self.assertEqual(s["unseen_shot_types"], {})

    def test_season_without_history_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "20212022"):
            evaluate_xg_model(_synthetic_shots((20212022,), per_season=50), test_seasons=[20212022], epsilon=1e-6)


class TestCli(unittest.TestCase):
    def test_evaluate_xg_writes_report_and_keeps_leading_zero_codes(self) -> None:
        from modeling.cli import _handle_evaluate_xg, build_parser

        shots = pd.concat(
            [
                _synthetic_shots((20232024, 20242025), per_season=1000),
                # Away net empty: as a number "0651" would become 651 and fail the four-digit check.
                _frame(_attempt(game_id=1, season_id=20242025, situation_code="0651")),
            ],
            ignore_index=True,
        )
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            shots.to_csv(tmp / "shots_train.csv", index=False)
            args = build_parser().parse_args(
                [
                    "evaluate-xg",
                    "--config",
                    "configs/modeling_smoke.yaml",
                    "--set",
                    "split.test_seasons=[20242025]",
                    "--shots",
                    str(tmp / "shots_train.csv"),
                    "--reports-root",
                    str(tmp / "reports"),
                ]
            )
            code = _handle_evaluate_xg(args)
            (report_dir,) = (tmp / "reports").iterdir()
            metrics = json.loads((report_dir / "metrics.json").read_text(encoding="utf-8"))
            summary = (report_dir / "summary.md").read_text(encoding="utf-8")
        self.assertTrue(report_dir.name.startswith("xg_"))
        self.assertEqual(code, 0)
        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["counts"]["excluded_empty_net"], 1)
        self.assertTrue(summary.startswith("status: ok\n"))
        self.assertIn("| 20242025 | 1000 | 1000 |", summary)


if __name__ == "__main__":
    unittest.main()
