"""Планировщик автообновления данных: ``pipeline/scheduled_sync.py`` (Задача 34).

Без моков ``subprocess``/``time`` (мандат ревьюера: мок вместо реальной
проверки — Critical): ``run_once`` гоняет настоящие процессы через
``sys.executable -c "..."`` во временном каталоге, ``check``/``seconds_until_next_run``
берут фиксированные ``datetime`` вместо системных часов.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Same sys.path pattern as tests/_pipeline_fixtures.py: pipeline/ modules are
# run with pipeline/ as cwd/sys.path root (no pipeline/__init__.py package).
_PIPELINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pipeline"))
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from scheduled_sync import (  # noqa: E402
    STALE_AFTER,
    SyncCommand,
    build_commands,
    check,
    run_once,
    seconds_until_next_run,
    sync_window,
)


class SyncWindowTest(unittest.TestCase):
    def test_ordinary_date(self) -> None:
        self.assertEqual(
            sync_window(date(2026, 9, 15)), ("2026-09-13", "2026-09-15")
        )

    def test_month_boundary(self) -> None:
        self.assertEqual(
            sync_window(date(2026, 10, 1)), ("2026-09-29", "2026-10-01")
        )

    def test_year_boundary(self) -> None:
        self.assertEqual(
            sync_window(date(2027, 1, 1)), ("2026-12-30", "2027-01-01")
        )


class SecondsUntilNextRunTest(unittest.TestCase):
    def test_before_sync_hour_same_day(self) -> None:
        now = datetime(2026, 9, 15, 7, 0, 0, tzinfo=timezone.utc)
        seconds = seconds_until_next_run(now)
        self.assertEqual(seconds, 3600.0)

    def test_exactly_at_sync_hour_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc)
        seconds = seconds_until_next_run(now)
        self.assertEqual(seconds, 24 * 3600.0)

    def test_after_sync_hour_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 9, 15, 9, 30, 0, tzinfo=timezone.utc)
        seconds = seconds_until_next_run(now)
        self.assertEqual(seconds, (24 - 1.5) * 3600.0)


class RunOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.status_file = self.tmp_dir / "sync_status.json"
        self.window = ("2026-09-13", "2026-09-15")

    def _marker_command(self, marker_name: str, exit_code: int = 0) -> SyncCommand:
        marker_path = self.tmp_dir / marker_name
        code = (
            f"from pathlib import Path; "
            f"Path({str(marker_path)!r}).write_text('done'); "
            f"import sys; sys.exit({exit_code})"
        )
        return SyncCommand(argv=[sys.executable, "-c", code], cwd=self.tmp_dir)

    def test_all_commands_succeed(self) -> None:
        cmd1 = self._marker_command("first.marker")
        cmd2 = self._marker_command("second.marker")

        ok = run_once([cmd1, cmd2], self.status_file, self.window)

        self.assertTrue(ok)
        self.assertTrue((self.tmp_dir / "first.marker").exists())
        self.assertTrue((self.tmp_dir / "second.marker").exists())

        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertIs(status["ok"], True)
        self.assertIsNone(status["failed_command"])
        self.assertIsNone(status["returncode"])
        self.assertEqual(status["window"], list(self.window))
        # finished_at parses back as a valid ISO UTC timestamp.
        datetime.fromisoformat(status["finished_at"])

    def test_first_command_fails_stops_chain(self) -> None:
        cmd1 = self._marker_command("first.marker", exit_code=3)
        cmd2 = self._marker_command("second.marker")

        ok = run_once([cmd1, cmd2], self.status_file, self.window)

        self.assertFalse(ok)
        self.assertTrue((self.tmp_dir / "first.marker").exists())
        self.assertFalse((self.tmp_dir / "second.marker").exists())

        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertIs(status["ok"], False)
        self.assertEqual(status["returncode"], 3)
        self.assertIn(sys.executable, status["failed_command"])


class BuildCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.window = ("2026-09-13", "2026-09-15")

    def test_without_digest(self) -> None:
        commands = build_commands(self.window, with_digest=False)

        self.assertEqual(len(commands), 1)
        loader_cmd = commands[0]
        self.assertEqual(
            loader_cmd.argv,
            [
                sys.executable,
                "-u",
                "load_season_modern.py",
                "--date-from",
                "2026-09-13",
                "--date-to",
                "2026-09-15",
            ],
        )
        self.assertEqual(loader_cmd.cwd.name, "pipeline")

    def test_with_digest(self) -> None:
        commands = build_commands(self.window, with_digest=True)

        self.assertEqual(len(commands), 2)
        digest_cmd = commands[1]
        self.assertEqual(digest_cmd.argv, [sys.executable, "-u", "push_digest_job.py"])
        self.assertEqual(digest_cmd.cwd.name, "telegram_bot")


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.status_file = Path(self._tmp.name) / "sync_status.json"
        self.now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)

    def _write_status(self, ok: bool, finished_at: datetime) -> None:
        self.status_file.write_text(
            json.dumps(
                {
                    "finished_at": finished_at.isoformat(),
                    "ok": ok,
                    "failed_command": None,
                    "returncode": None,
                    "window": ["2026-09-13", "2026-09-15"],
                }
            ),
            encoding="utf-8",
        )

    def test_no_file_is_unhealthy(self) -> None:
        self.assertEqual(check(self.status_file, self.now), 1)

    def test_ok_and_fresh_is_healthy(self) -> None:
        self._write_status(ok=True, finished_at=self.now - timedelta(hours=1))
        self.assertEqual(check(self.status_file, self.now), 0)

    def test_ok_but_stale_is_unhealthy(self) -> None:
        self._write_status(ok=True, finished_at=self.now - STALE_AFTER - timedelta(minutes=1))
        self.assertEqual(check(self.status_file, self.now), 1)

    def test_failed_run_is_unhealthy_even_if_fresh(self) -> None:
        self._write_status(ok=False, finished_at=self.now - timedelta(minutes=1))
        self.assertEqual(check(self.status_file, self.now), 1)


if __name__ == "__main__":
    unittest.main()
