"""Ежедневная публикация прогноза: ``pipeline/scheduled_publish.py`` (Задача 73).

Цепочка проверяется по аргументам команд и по моделям, которые она реально загрузит из
``deploy/models/`` (они приезжают в прод-образе); прогон цепочки и предусловие sync —
общие с retrain и покрыты ``test_scheduled_retrain.py``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib

_PIPELINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pipeline"))
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from scheduled_publish import MODELS_ROOT, build_commands, needs_catch_up  # noqa: E402
from scheduled_retrain import PRECONDITION_FAILED  # noqa: E402

from modeling.artifacts import resolve_latest_model_dir  # noqa: E402
from modeling.cli import build_parser  # noqa: E402
from modeling.feature_schema import features_hash  # noqa: E402

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class BuildCommandsTest(unittest.TestCase):
    def test_dataset_then_win_forecast_then_both_publications(self) -> None:
        steps = [cmd.argv[cmd.argv.index("modeling.cli") + 1 :] for cmd in build_commands()]
        self.assertEqual([s[0] for s in steps], ["build-dataset", "predict", "publish-predictions", "publish-goals"])
        dataset, predict, publish_win, publish_goals = steps
        self.assertEqual(_arg(dataset, "--mode"), "predict")
        self.assertEqual(_arg(dataset, "--min-prior-games"), "5")
        # Every step reads the models shipped in the image, not artifacts/.
        self.assertTrue(_arg(dataset, "--train-metadata-path").startswith(f"{MODELS_ROOT}/"))
        for step in (predict, publish_win, publish_goals):
            self.assertEqual(_arg(step, "--artifacts-root"), MODELS_ROOT)
        self.assertEqual(_arg(predict, "--output"), _arg(publish_win, "--predictions"))
        self.assertEqual(_arg(predict, "--predict-dataset"), _arg(publish_goals, "--predict-dataset"))
        self.assertTrue(all(cmd.cwd == REPO_ROOT for cmd in build_commands()))

    def test_dataset_is_built_with_drop_policy_and_five_prior_games(self) -> None:
        # The ≥ 5 games rule of the preview lives in the dataset's cold-start policy
        # (the drop itself — test_modeling_dataset_build.test_cold_start_policy).
        (dataset, *_rest) = build_commands()
        args = build_parser().parse_args(dataset.argv[dataset.argv.index("modeling.cli") + 1 :])
        self.assertEqual(args.cold_start_policy_predict, "drop")
        self.assertEqual(args.min_prior_games, 5)


class ShippedModelsTest(unittest.TestCase):
    def test_win_and_goal_models_resolve_and_load(self) -> None:
        root = REPO_ROOT / MODELS_ROOT
        win = resolve_latest_model_dir(root, "home_win", "logreg")
        metadata = json.loads((win / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "ok")
        # The daily build (CLI defaults, drop policy) must reproduce the model's hash,
        # otherwise build-dataset fails on features_hash mismatch every day.
        defaults = build_parser().parse_args(["build-dataset", "--mode", "predict"])
        expected_hash = features_hash(
            feature_manifest=metadata["feature_manifest"],
            rolling_windows=defaults.rolling_windows,
            cold_start_policy=f"train:drop|predict:{defaults.cold_start_policy_predict}",
            feature_set_version=defaults.feature_set_version,
        )
        self.assertEqual(metadata["features_hash"], expected_hash)
        self.assertTrue(hasattr(joblib.load(win / "model.joblib"), "predict_proba"))
        goals = resolve_latest_model_dir(root, "over_5_5", "goals")
        goal_metadata = json.loads((goals / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(goals.parent.name, goal_metadata["run_id"])
        self.assertTrue(hasattr(joblib.load(goals / "model.joblib"), "lambdas"))


class NeedsCatchUpTest(unittest.TestCase):
    def test_no_status_older_than_a_day_or_precondition_refusal_runs_at_start(self) -> None:
        def write(hours_ago: int, failed_command: str | None = None) -> None:
            finished_at = (NOW - timedelta(hours=hours_ago)).isoformat()
            status.write_text(json.dumps({"finished_at": finished_at, "failed_command": failed_command}), encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp_name:
            status = Path(tmp_name) / "publish_status.json"
            self.assertTrue(needs_catch_up(status, NOW))
            write(23)
            self.assertFalse(needs_catch_up(status, NOW))
            write(25)
            self.assertTrue(needs_catch_up(status, NOW))
            # A refusal on a stale sync published nothing: a restart publishes right away.
            write(1, PRECONDITION_FAILED)
            self.assertTrue(needs_catch_up(status, NOW))


if __name__ == "__main__":
    unittest.main()
