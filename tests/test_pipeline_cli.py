"""CLI entry point of ``pipeline/load_season_modern.py`` (Задача 33).

Covers exactly the two behaviours introduced by moving to a single
``SEASON_ID`` source: ``main()`` fails loudly (``RuntimeError`` naming the
variable) when no season_id is resolved from either ``.env``/``config.py`` or
``--season-id``, and the season label stored on ``games.season`` is always
derived from whatever season_id `main()` ends up with — there is no more
separate ``--current-season`` flag. ``ModernNhlLoader.run()`` is stubbed in
every test here: nothing in this module touches the network or the DB.
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

    def test_uses_config_season_id_and_derives_label_when_no_cli_override(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", 20262027):
            loader.main([])

        self.assertEqual(len(self.run_calls), 1)
        instance = self.run_calls[0]
        self.assertEqual(instance.season_id, 20262027)
        self.assertEqual(instance.current_season_label, "26/27")

    def test_cli_season_id_override_derives_its_own_label(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", None):
            loader.main(["--season-id", "20242025"])

        self.assertEqual(len(self.run_calls), 1)
        instance = self.run_calls[0]
        self.assertEqual(instance.season_id, 20242025)
        self.assertEqual(instance.current_season_label, "24/25")

    def test_current_season_cli_flag_was_removed(self) -> None:
        with mock.patch.object(loader.config, "SEASON_ID", 20262027):
            with self.assertRaises(SystemExit):
                loader.main(["--current-season", "26/27"])

        self.assertEqual(self.run_calls, [])


if __name__ == "__main__":
    unittest.main()
