"""CLI entry point of ``pipeline/load_season_modern.py`` (Задача 33).

Covers the behaviours introduced by moving to a single ``SEASON_ID`` source:
``main()`` fails loudly (``RuntimeError`` naming the variable) when no
season_id is resolved from either ``.env``/``config.py`` or ``--season-id``;
the season label stored on ``games.season`` is always derived from whatever
season_id `main()` ends up with (no more separate ``--current-season`` flag);
and — fix round 1, GC4 — the *default* window start (``start_date``) follows
that same final season_id, not whatever ``config.py`` derived from
``config.SEASON_ID`` at import time, which used to go stale the moment
``--season-id`` overrode it (silent `""` window, or a window for the wrong
season entirely). ``ModernNhlLoader.run()`` is stubbed in every test here:
nothing in this module touches the network or the DB.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests._pipeline_fixtures import loader


class MainSeasonResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.run_calls = []

        def _fake_run(instance):
            self.run_calls.append(instance)

        patcher = mock.patch.object(loader.ModernNhlLoader, "run", _fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_raises_with_variable_name_when_season_id_missing_everywhere(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", None):
            with self.assertRaises(RuntimeError) as ctx:
                loader.main([])

        self.assertIn("SEASON_ID", str(ctx.exception))
        self.assertEqual(self.run_calls, [])

    def test_uses_config_season_id_and_derives_label_and_window_when_no_cli_override(
        self,
    ) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", 20262027), mock.patch.object(
            loader.config, "DATE_FROM", None
        ):
            loader.main([])

        self.assertEqual(len(self.run_calls), 1)
        instance = self.run_calls[0]
        self.assertEqual(instance.season_id, 20262027)
        self.assertEqual(instance.current_season_label, "26/27")
        self.assertEqual(instance.start_date, "2026-09-01")

    def test_cli_season_id_override_derives_its_own_label_and_window(self) -> None:
        """Fix round 1, case (a): SEASON_ID unset, only `--season-id` given —
        the window must start from *that* season, not from `""` (which used
        to reach the NHL API as `gameDate>=""`, an unintended window instead
        of a startup failure)."""
        with mock.patch.object(loader.config, "SEASON_ID", None), mock.patch.object(
            loader.config, "DATE_FROM", None
        ):
            loader.main(["--season-id", "20242025"])

        self.assertEqual(len(self.run_calls), 1)
        instance = self.run_calls[0]
        self.assertEqual(instance.season_id, 20242025)
        self.assertEqual(instance.current_season_label, "24/25")
        self.assertEqual(instance.start_date, "2024-09-01")

    def test_cli_season_id_override_recomputes_window_over_config_season_id(self) -> None:
        """Fix round 1, case (b): `.env` has SEASON_ID=20262027, but
        `--season-id 20242025` overrides which season is actually loaded —
        the window must follow the override (24/25), not the season derived
        from `config.SEASON_ID` at import time (26/27), or the query would
        silently return zero games (season_id=24/25, gameDate from 26/27)."""
        with mock.patch.object(loader.config, "SEASON_ID", 20262027), mock.patch.object(
            loader.config, "DATE_FROM", None
        ):
            loader.main(["--season-id", "20242025"])

        self.assertEqual(len(self.run_calls), 1)
        instance = self.run_calls[0]
        self.assertEqual(instance.season_id, 20242025)
        self.assertEqual(instance.start_date, "2024-09-01")

    def test_explicit_date_from_flag_wins_over_derived_window(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", 20262027), mock.patch.object(
            loader.config, "DATE_FROM", None
        ):
            loader.main(["--date-from", "2025-01-01"])

        instance = self.run_calls[0]
        self.assertEqual(instance.start_date, "2025-01-01")

    def test_explicit_date_from_env_wins_over_derived_window(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", 20262027), mock.patch.object(
            loader.config, "DATE_FROM", "2025-06-01"
        ):
            loader.main([])

        instance = self.run_calls[0]
        self.assertEqual(instance.start_date, "2025-06-01")

    def test_current_season_cli_flag_was_removed(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", 20262027):
            with self.assertRaises(SystemExit):
                loader.main(["--current-season", "26/27"])

        self.assertEqual(self.run_calls, [])


if __name__ == "__main__":
    unittest.main()
