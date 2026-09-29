"""Планировщик retrain: ``pipeline/scheduled_retrain.py`` (Задача 26B).

Как в ``test_scheduled_sync.py``: без моков — настоящие процессы через
``sys.executable -c`` во временном каталоге, фиксированные ``datetime`` вместо часов.
Гейт (провал -> ``latest`` прежний) и ``promote`` покрыты тестами 26A.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PIPELINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pipeline"))
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

import scheduled_sync  # noqa: E402
from scheduled_retrain import (  # noqa: E402
    MIN_INTERVAL,
    PRECONDITION_FAILED,
    STALE_AFTER,
    build_commands,
    needs_catch_up,
    run_retrain,
    seconds_until_next_run,
    should_run,
)
from scheduled_sync import SyncCommand  # noqa: E402

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)  # вторник


def _write_status(
    path: Path, finished_at: datetime, ok: bool = True, failed_command: str | None = None
) -> None:
    path.write_text(
        json.dumps(
            {"finished_at": finished_at.isoformat(), "ok": ok, "failed_command": failed_command}
        ),
        encoding="utf-8",
    )


class BuildCommandsTest(unittest.TestCase):
    def test_dataset_first_then_train_home_win_without_promote(self) -> None:
        dataset_cmd, train_cmd = build_commands()

        self.assertIn("build-dataset", dataset_cmd.argv)
        self.assertEqual(dataset_cmd.argv[dataset_cmd.argv.index("--mode") + 1], "train")
        self.assertIn("train", train_cmd.argv)
        self.assertNotIn("build-dataset", train_cmd.argv)
        self.assertEqual(train_cmd.argv[train_cmd.argv.index("--task") + 1], "home_win")
        self.assertIn("--no-promote", train_cmd.argv)
        self.assertEqual(dataset_cmd.cwd, train_cmd.cwd)
        self.assertTrue((dataset_cmd.cwd / "modeling" / "cli.py").exists())


class SecondsUntilNextRunTest(unittest.TestCase):
    def test_before_slot_same_day(self) -> None:
        now = datetime(2026, 9, 28, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(seconds_until_next_run(now), 3600.0)

    def test_exactly_at_slot_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(seconds_until_next_run(now), 24 * 3600.0)

    def test_after_slot_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 9, 28, 13, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(seconds_until_next_run(now), 23 * 3600.0)

    def test_next_target_uses_daily_slot_without_double_fire(self) -> None:
        target = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        next_target, sleep_seconds = scheduled_sync._next_target(
            target - timedelta(microseconds=1), target, seconds_until_next_run
        )
        self.assertEqual(next_target, datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(sleep_seconds, 24 * 3600.0)


class NeedsCatchUpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.status = Path(self._tmp.name) / "retrain_status.json"

    def test_no_status_needs_catch_up(self) -> None:
        self.assertTrue(needs_catch_up(self.status, NOW))

    def test_young_status_does_not(self) -> None:
        _write_status(self.status, NOW - MIN_INTERVAL + timedelta(hours=1))
        self.assertFalse(needs_catch_up(self.status, NOW))

    def test_young_failed_status_does_not(self) -> None:
        _write_status(self.status, NOW - timedelta(days=1), ok=False)
        self.assertFalse(needs_catch_up(self.status, NOW))

    def test_precondition_refusal_does_not_suppress_catch_up(self) -> None:
        _write_status(
            self.status, NOW - timedelta(hours=1), ok=False, failed_command=PRECONDITION_FAILED
        )
        self.assertTrue(needs_catch_up(self.status, NOW))

    def test_old_status_needs_catch_up(self) -> None:
        _write_status(self.status, NOW - MIN_INTERVAL - timedelta(hours=1))
        self.assertTrue(needs_catch_up(self.status, NOW))


class ShouldRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.status = Path(self._tmp.name) / "retrain_status.json"
        self.monday = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.wednesday = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)

    def test_monday_runs_even_with_fresh_status(self) -> None:
        _write_status(self.status, self.monday - timedelta(days=7, minutes=-1))
        self.assertTrue(should_run(self.status, self.monday))

    def test_non_monday_with_fresh_status_skips(self) -> None:
        _write_status(self.status, self.wednesday - timedelta(days=2))
        self.assertFalse(should_run(self.status, self.wednesday))

    def test_non_monday_with_refused_status_retries(self) -> None:
        _write_status(
            self.status, self.wednesday - timedelta(days=1), ok=False,
            failed_command=PRECONDITION_FAILED,
        )
        self.assertTrue(should_run(self.status, self.wednesday))


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.status = Path(self._tmp.name) / "retrain_status.json"

    def _check(self) -> int:
        return scheduled_sync.check(self.status, NOW, STALE_AFTER)

    def test_missing_file(self) -> None:
        self.assertEqual(self._check(), 1)

    def test_failed_run(self) -> None:
        _write_status(self.status, NOW, ok=False)
        self.assertEqual(self._check(), 1)

    def test_older_than_eight_days(self) -> None:
        _write_status(self.status, NOW - timedelta(days=8, minutes=1))
        self.assertEqual(self._check(), 1)

    def test_fresh_ok(self) -> None:
        _write_status(self.status, NOW - timedelta(days=7))
        self.assertEqual(self._check(), 0)


class RunRetrainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.status = self.tmp_dir / "retrain_status.json"
        self.sync_status = self.tmp_dir / "sync_status.json"

    def _marker_command(self, marker_name: str, exit_code: int = 0) -> SyncCommand:
        marker_path = self.tmp_dir / marker_name
        code = (
            f"from pathlib import Path; "
            f"Path({str(marker_path)!r}).write_text('done'); "
            f"import sys; sys.exit({exit_code})"
        )
        return SyncCommand(argv=[sys.executable, "-c", code], cwd=self.tmp_dir)

    def _assert_refused(self) -> None:
        first = self._marker_command("first.marker")
        ok = run_retrain([first], self.status, self.sync_status, NOW)

        self.assertFalse(ok)
        self.assertFalse((self.tmp_dir / "first.marker").exists())
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("precondition", data["failed_command"])

    def test_missing_sync_status_blocks_chain(self) -> None:
        self._assert_refused()

    def test_failed_sync_status_blocks_chain(self) -> None:
        _write_status(self.sync_status, NOW - timedelta(hours=1), ok=False)
        self._assert_refused()

    def test_stale_sync_status_blocks_chain(self) -> None:
        _write_status(self.sync_status, NOW - scheduled_sync.STALE_AFTER - timedelta(minutes=1))
        self._assert_refused()

    def test_fresh_sync_runs_chain_in_order(self) -> None:
        _write_status(self.sync_status, NOW - timedelta(hours=1))
        ok = run_retrain(
            [self._marker_command("first.marker"), self._marker_command("second.marker")],
            self.status,
            self.sync_status,
            NOW,
        )

        self.assertTrue(ok)
        self.assertTrue((self.tmp_dir / "first.marker").exists())
        self.assertTrue((self.tmp_dir / "second.marker").exists())
        self.assertTrue(json.loads(self.status.read_text(encoding="utf-8"))["ok"])

    def test_failed_step_stops_chain(self) -> None:
        _write_status(self.sync_status, NOW - timedelta(hours=1))
        ok = run_retrain(
            [self._marker_command("first.marker", exit_code=3), self._marker_command("second.marker")],
            self.status,
            self.sync_status,
            NOW,
        )

        self.assertFalse(ok)
        self.assertFalse((self.tmp_dir / "second.marker").exists())
        data = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["returncode"], 3)


if __name__ == "__main__":
    unittest.main()
