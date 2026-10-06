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
    _next_target,
    build_commands,
    check,
    is_digest_slot,
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


def _utc(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 9, day, hour, minute, 0, tzinfo=timezone.utc)


class SecondsUntilNextRunTest(unittest.TestCase):
    def test_night_steps_every_half_hour(self) -> None:
        self.assertEqual(seconds_until_next_run(_utc(2, 10)), 20 * 60.0)
        self.assertEqual(seconds_until_next_run(_utc(2, 30)), 30 * 60.0)

    def test_evening_slot_before_midnight_steps_into_next_day(self) -> None:
        self.assertEqual(seconds_until_next_run(_utc(23, 45)), 15 * 60.0)

    def test_last_night_slot_is_digest_hour(self) -> None:
        self.assertEqual(seconds_until_next_run(_utc(7, 30)), 30 * 60.0)

    def test_exactly_at_digest_slot_skips_the_day_to_evening(self) -> None:
        self.assertEqual(seconds_until_next_run(_utc(8, 0)), 10 * 3600.0)

    def test_daytime_waits_for_evening_start(self) -> None:
        self.assertEqual(seconds_until_next_run(_utc(12, 5)), (5 * 60 + 55) * 60.0)

    def test_just_before_evening_start(self) -> None:
        self.assertEqual(seconds_until_next_run(_utc(17, 59)), 60.0)

    def test_night_has_29_slots_from_18_to_08_inclusive(self) -> None:
        """Сутки по сетке: 18:00…23:30 (12) + 00:00…08:00 (17) = 29 прогонов."""
        moment, slots = _utc(8, 0), []
        while moment < _utc(8, 0, day=16):
            moment += timedelta(seconds=seconds_until_next_run(moment))
            slots.append(moment)
        self.assertEqual(len(slots), 29)
        self.assertEqual(slots[0], _utc(18, 0))
        self.assertEqual(slots[-1], _utc(8, 0, day=16))


class IsDigestSlotTest(unittest.TestCase):
    def test_only_the_last_night_slot_sends_the_digest(self) -> None:
        self.assertTrue(is_digest_slot(_utc(8, 0)))
        for hour, minute in ((18, 0), (0, 0), (7, 30), (8, 30)):
            self.assertFalse(is_digest_slot(_utc(hour, minute)), (hour, minute))

    def test_slot_reached_through_next_target_is_exact(self) -> None:
        """``loop`` решает про дайджест по ``target`` из ``_next_target``: точка
        должна совпасть с сеткой до микросекунды, иначе 08:00 не узнается."""
        now = _utc(7, 30) + timedelta(microseconds=123457)
        target, _ = _next_target(now, target=now)
        self.assertTrue(is_digest_slot(target))


class NextTargetTest(unittest.TestCase):
    """Fix round 1: ``loop`` must not fire the same slot twice.

    Recomputing purely from ``now`` after each run let an imprecise
    ``time.sleep`` (or wall clock reading a hair behind its target) wake the
    loop just before a slot: ``now`` still looked "before the slot", so the
    very next iteration scheduled again almost immediately and fired a second
    run (and digest) for the same slot. ``_next_target`` fixes this by
    computing from ``max(now, target)`` — see its docstring.
    """

    def test_normal_case_before_digest_slot(self) -> None:
        now = _utc(7, 0)
        next_target, sleep_seconds = _next_target(now, target=now)

        self.assertEqual(sleep_seconds, 30 * 60.0)
        self.assertEqual(next_target, _utc(7, 30))

    def test_early_wake_just_before_target_does_not_double_fire(self) -> None:
        """``now`` reads a hair before ``target`` (the digest slot that just ran) —
        must advance to the evening slot, not fire almost immediately again."""
        target = _utc(8, 0)
        now = target - timedelta(microseconds=1)

        next_target, sleep_seconds = _next_target(now, target)

        self.assertEqual(sleep_seconds, 10 * 3600.0)
        self.assertEqual(next_target, _utc(18, 0))

    def test_now_exactly_at_target_does_not_double_fire(self) -> None:
        target = _utc(2, 30)

        next_target, sleep_seconds = _next_target(now=target, target=target)

        self.assertEqual(sleep_seconds, 30 * 60.0)
        self.assertEqual(next_target, _utc(3, 0))

    def test_long_pause_catches_up_from_now_not_stale_target(self) -> None:
        """Container paused for days: ``now`` is far past ``target`` — the next
        slot must follow the actual current time, not replay from the past."""
        target = _utc(8, 0)
        now = _utc(10, 0, day=18)

        next_target, sleep_seconds = _next_target(now, target)

        self.assertEqual(sleep_seconds, 8 * 3600.0)
        self.assertEqual(next_target, _utc(18, 0, day=18))


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
