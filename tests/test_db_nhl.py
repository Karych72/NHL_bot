"""PostgreSQL schema and loader data integrity (optional).

* Schema: set RUN_DB_SCHEMA_TESTS=1 (``make test-db`` does this) and reachable PG from config.
* Data: set RUN_DB_DATA_TESTS=1 after loading (e.g. ``make season-sync-month``).

Once RUN_DB_SCHEMA_TESTS is on, TestNhlSchema fails loudly (error/failure, not
skip) on an unreachable server or a missing table — the caller asked for these
checks, so "couldn't check" must not read as "passed" (see CLAUDE.md Global
Constraint 4, падать громко). Only TestNhlLoadedData (opt-in via
RUN_DB_DATA_TESTS, never run in CI) still skips on missing prerequisites, since
that flag is a developer choosing to check loaded data, not a CI gate.

Requires: psycopg2, .env or env vars PG_* matching the loader.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TELEGRAM_BOT = os.path.join(REPO_ROOT, "telegram_bot")
if TELEGRAM_BOT not in sys.path:
    sys.path.insert(0, TELEGRAM_BOT)

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

import config  # noqa: E402


def _connect():
    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        user=config.PG_USER,
        database=config.PG_DATABASE,
    )


def _base_table_exists(cur, relname: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = %s
        """,
        (relname,),
    )
    return cur.fetchone() is not None


def _pk_columns(cur, table: str):
    """Primary-key column names in index order."""
    cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
            AND NOT a.attisdropped
        WHERE n.nspname = 'public'
          AND c.relname = %s
          AND i.indisprimary
        ORDER BY k.ord
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _unique_partial_index_predicate_row(cur, table: str, index_name: str):
    """Raw fetchone() for a UNIQUE index's WHERE-predicate lookup.

    Returns ``None`` when no such unique index exists at all; a 1-tuple
    ``(predicate_text_or_None,)`` when it does — the inner ``None`` means the
    index exists but is not partial (``pg_get_expr(NULL, ...)`` on a
    non-partial index's null ``indpred``). Callers must distinguish these two
    ``None``s (missing index vs. non-partial index) rather than collapsing
    both into one "missing" message.
    """
    cur.execute(
        """
        SELECT pg_get_expr(i.indpred, i.indrelid)
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class tc ON tc.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = tc.relnamespace
        WHERE n.nspname = 'public'
          AND tc.relname = %s
          AND ic.relname = %s
          AND i.indisunique
        """,
        (table, index_name),
    )
    return cur.fetchone()


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestNhlSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # RUN_DB_SCHEMA_TESTS was explicitly requested (see class decorator above),
        # so an unreachable server is a hard error, not a skip.
        cls.conn = _connect()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn") and cls.conn:
            cls.conn.close()

    def test_primary_keys(self):
        with self.conn.cursor() as cur:
            if not _base_table_exists(cur, "teams"):
                self.fail("public.teams missing — run: make db-reset-local (or check DDL)")
            pk = _pk_columns(cur, "teams")
            if not pk:
                self.fail(
                    "teams has no PRIMARY KEY — run `make db-reset-local` "
                    "to apply data_tables/t.*.sql"
                )
            self.assertEqual(pk, ["team_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "teams_stats"), ["team_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "rosters"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "players_season_stats"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "goalies_season_stats"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "players_advanced_stats"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "players_shot_types"), ["player_id", "season_id"])
            self.assertEqual(_pk_columns(cur, "games"), ["game_id"])
            self.assertEqual(_pk_columns(cur, "scheduled_games"), ["game_id"])
            self.assertEqual(_pk_columns(cur, "game_predictions"), ["game_id", "task"])
            self.assertEqual(
                _pk_columns(cur, "prediction_log"), ["game_id", "task", "run_id", "published_on"]
            )
            self.assertEqual(_pk_columns(cur, "shot_attempts"), ["game_id", "event_id"])

    def test_games_has_season_id(self):
        with self.conn.cursor() as cur:
            if not _base_table_exists(cur, "games"):
                self.fail("public.games missing — run: make db-reset-local (or check DDL)")
            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = 'games'
                  AND a.attname = 'season_id' AND NOT a.attisdropped
                """
            )
            row = cur.fetchone()
            self.assertIsNotNone(
                row,
                "games.season_id missing — run: make db-reset-local",
            )
            self.assertTrue(
                str(row[0]).startswith("bigint"),
                f"expected bigint season_id, got {row[0]!r}",
            )

    def test_bot_subscriptions_migration_applied(self):
        with self.conn.cursor() as cur:
            if not _base_table_exists(cur, "bot_subscriptions"):
                self.fail(
                    "public.bot_subscriptions missing — run: make db-migrate "
                    "(data_tables/migrations/0001_bot_subscriptions.up.sql)"
                )
            self.assertEqual(_pk_columns(cur, "bot_subscriptions"), ["id"])

            digest_row = _unique_partial_index_predicate_row(
                cur, "bot_subscriptions", "bot_subscriptions_digest_unique"
            )
            self.assertIsNotNone(
                digest_row,
                "bot_subscriptions_digest_unique missing — run: make db-migrate",
            )
            self.assertIsNotNone(
                digest_row[0],
                "bot_subscriptions_digest_unique exists but is not partial — "
                "run: make db-migrate",
            )
            self.assertIn("morning_digest", digest_row[0])

            team_row = _unique_partial_index_predicate_row(
                cur, "bot_subscriptions", "bot_subscriptions_team_unique"
            )
            self.assertIsNotNone(
                team_row, "bot_subscriptions_team_unique missing — run: make db-migrate"
            )
            self.assertIsNotNone(
                team_row[0],
                "bot_subscriptions_team_unique exists but is not partial — "
                "run: make db-migrate",
            )
            self.assertIn("team_scores", team_row[0])

            if not _base_table_exists(cur, "schema_migrations"):
                self.fail("public.schema_migrations missing — run: make db-migrate")
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = '0001'")
            self.assertIsNotNone(
                cur.fetchone(),
                "schema_migrations missing version 0001 — run: make db-migrate",
            )

            # Миграция 0005 (Задача 60): отметка отправки вместо timezone.
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'bot_subscriptions'"
            )
            columns = dict(cur.fetchall())
            self.assertEqual(
                columns.get("last_sent_night"),
                "date",
                "bot_subscriptions without last_sent_night — run: make db-migrate",
            )
            self.assertNotIn("timezone", columns)

            # Миграция 0007 (Задача 63): времени доставки у подписки больше нет.
            self.assertNotIn("digest_time", columns, "run: make db-migrate")
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = '0007'")
            self.assertIsNotNone(
                cur.fetchone(), "schema_migrations missing version 0007 — run: make db-migrate"
            )

            # Миграция 0006 (Задача 61): подписка на страну.
            self.assertEqual(columns.get("country"), "text")
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = '0006'")
            self.assertIsNotNone(
                cur.fetchone(), "schema_migrations missing version 0006 — run: make db-migrate"
            )


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestSubscriptionRepoSql(unittest.TestCase):
    """SQL ``subscription_repo`` на настоящей ``bot_subscriptions`` (Задача 60).

    Юнит-тесты рассылки подменяют репозиторий; здесь проверяется, что сами запросы
    выбирают и отмечают нужные строки — ошибка в ``WHERE`` у ``mark_digest_night_sent``
    слала бы дайджест каждые полчаса при зелёных юнит-тестах. Строки заводятся на
    отрицательные ``chat_id``, которых нет среди подписчиков (диапазон групп Telegram,
    но таких групп у бота нет), и удаляются до и после теста.
    """

    CHAT = -999000060
    # Соседний подписчик: отметка CHAT не должна задеть чужую строку.
    OTHER_CHAT = -999000061
    TEAM = 6
    NIGHT = date(2026, 10, 6)

    def setUp(self):
        import subscription_repo

        self.repo = subscription_repo
        self._delete_rows()
        self.addCleanup(self._delete_rows)

    def _delete_rows(self):
        # psycopg2: `with conn` — транзакция, а не закрытие; закрывает `closing`.
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            for table in ("bot_subscriptions", "bot_chat_game_sent"):
                cur.execute(
                    f"DELETE FROM {table} WHERE chat_id IN (%s, %s)", (self.CHAT, self.OTHER_CHAT)
                )

    def _digest_rows(self, chat_id=CHAT):
        return [r for r in self.repo.list_active_morning_digest_rows() if r[0] == chat_id]

    def _game_rows(self, chat_id=CHAT):
        """Строки рассылки итогов по матчу (команды и страны) чата: (team_id, country)."""
        return [r[1:] for r in self.repo.list_active_game_rows() if r[0] == chat_id]

    def _last_sent(self, chat_id, kind):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_sent_night FROM bot_subscriptions WHERE chat_id = %s AND kind = %s",
                (chat_id, kind),
            )
            return cur.fetchone()[0]

    def test_digest_lifecycle(self):
        repo = self.repo
        repo.upsert_chat_kind(self.CHAT, "morning_digest")
        repo.upsert_chat_kind(self.CHAT, "morning_digest")
        self.assertEqual(self._digest_rows(), [(self.CHAT, None)], "повторный вызов не плодит строки")

        repo.mark_subscription_inactive_by_chat_kind_team(self.CHAT, "morning_digest")
        self.assertEqual(self._digest_rows(), [], "погашенная подписка в рассылке")
        # Повторная подписка реактивирует ту же строку.
        repo.upsert_chat_kind(self.CHAT, "morning_digest")
        self.assertEqual(self._digest_rows(), [(self.CHAT, None)])

    def test_mark_digest_night_sent_touches_only_its_subscription(self):
        repo = self.repo
        for chat_id in (self.CHAT, self.OTHER_CHAT):
            repo.upsert_chat_kind(chat_id, "morning_digest")
            repo.upsert_team_scores(chat_id, self.TEAM)

        repo.mark_digest_night_sent(self.CHAT, self.NIGHT)
        self.assertEqual(self._digest_rows(), [(self.CHAT, self.NIGHT)])
        self.assertIsNone(self._last_sent(self.CHAT, "team_scores"))
        self.assertEqual(self._digest_rows(self.OTHER_CHAT), [(self.OTHER_CHAT, None)])

    def test_country_subscriptions(self):
        repo = self.repo
        repo.upsert_country(self.CHAT, "RUS")
        repo.upsert_country(self.CHAT, "FIN")
        repo.upsert_team_scores(self.CHAT, self.TEAM)
        repo.upsert_country(self.OTHER_CHAT, "RUS")
        repo.upsert_chat_kind(self.CHAT, "morning_digest")
        # Задача 85: рассылка по матчу читает команды и страны чата одним списком.
        self.assertEqual(self._game_rows(), [(None, "FIN"), (None, "RUS"), (self.TEAM, None)])
        self.assertEqual(self._game_rows(self.OTHER_CHAT), [(None, "RUS")])

        # Отписка от одной страны; переподписка реактивирует ту же строку.
        repo.mark_subscription_inactive_by_chat_kind_team(self.CHAT, "country_players", None, "RUS")
        self.assertEqual(repo.get_chat_subscriptions(self.CHAT), (True, False, {self.TEAM}, ["FIN"]))
        repo.upsert_country(self.CHAT, "RUS")
        self.assertEqual(
            repo.get_chat_subscriptions(self.CHAT), (True, False, {self.TEAM}, ["FIN", "RUS"])
        )

    def test_chat_game_marks_and_deactivate_chat(self):
        """Задача 85: отметка «итог матча ушёл» — по паре (чат, матч), повторная — no-op;
        отметка превью (``bot_game_sent``) итогом не считается. Заблокировавший бота чат
        гасит все свои подписки, чужие живы."""
        repo = self.repo
        game, other_game = 2099990001, 2099990002
        repo.mark_chat_games_sent(self.CHAT, [game, other_game])
        repo.mark_chat_games_sent(self.CHAT, [game])
        repo.mark_chat_games_sent(self.OTHER_CHAT, [other_game])
        self.assertEqual(repo.chat_games_sent([game]), {(self.CHAT, game)})
        self.assertEqual(
            repo.chat_games_sent([game, other_game]),
            {(self.CHAT, game), (self.CHAT, other_game), (self.OTHER_CHAT, other_game)},
        )

        repo.upsert_chat_kind(self.OTHER_CHAT, "upcoming_games")
        ((upcoming, _),) = [r for r in repo.list_active_upcoming_rows() if r[1] == self.OTHER_CHAT]
        repo.mark_game_sent(upcoming, game)
        self.assertNotIn((self.OTHER_CHAT, game), repo.chat_games_sent([game]))

        for chat_id in (self.CHAT, self.OTHER_CHAT):
            repo.upsert_chat_kind(chat_id, "morning_digest")
            repo.upsert_country(chat_id, "RUS")
        repo.upsert_team_scores(self.CHAT, self.TEAM)
        repo.deactivate_chat(self.CHAT)
        self.assertEqual(repo.get_chat_subscriptions(self.CHAT), (False, False, set(), []))
        self.assertEqual(repo.get_chat_subscriptions(self.OTHER_CHAT), (True, True, set(), ["RUS"]))

    def test_get_chat_subscriptions_reads_only_active_rows_of_the_chat(self):
        repo = self.repo
        self.assertEqual(repo.get_chat_subscriptions(self.CHAT), (False, False, set(), []))
        repo.upsert_chat_kind(self.CHAT, "morning_digest")
        repo.upsert_team_scores(self.CHAT, self.TEAM)
        repo.upsert_team_scores(self.CHAT, self.TEAM + 1)
        repo.upsert_country(self.CHAT, "RUS")
        repo.upsert_chat_kind(self.OTHER_CHAT, "morning_digest")
        repo.deactivate_team_scores(self.CHAT, self.TEAM + 1)
        self.assertEqual(repo.get_chat_subscriptions(self.CHAT), (True, False, {self.TEAM}, ["RUS"]))
        repo.mark_subscription_inactive_by_chat_kind_team(self.CHAT, "morning_digest")
        self.assertEqual(repo.get_chat_subscriptions(self.CHAT), (False, False, {self.TEAM}, ["RUS"]))

    def test_upcoming_games_subscription(self):
        """Задача 78: подписка на чат — одна строка, переподписка реактивирует её; в
        рассылку идут только активные; отметка матча — в ``bot_game_sent``, своя у каждой
        подписки."""
        repo = self.repo
        repo.upsert_chat_kind(self.CHAT, "upcoming_games")
        repo.upsert_chat_kind(self.CHAT, "upcoming_games")
        repo.upsert_chat_kind(self.OTHER_CHAT, "upcoming_games")
        repo.upsert_country(self.CHAT, "RUS")
        ((upcoming, _),) = [r for r in repo.list_active_upcoming_rows() if r[1] == self.CHAT]
        ((other, _),) = [r for r in repo.list_active_upcoming_rows() if r[1] == self.OTHER_CHAT]
        self.assertEqual(repo.get_chat_subscriptions(self.CHAT), (False, True, set(), ["RUS"]))

        game = 2099990003
        repo.mark_game_sent(upcoming, game)
        self.assertEqual(repo.sent_games([game]), {(upcoming, game)})
        self.assertNotIn((other, game), repo.sent_games([game]))

        repo.mark_subscription_inactive_by_chat_kind_team(self.CHAT, "upcoming_games")
        self.assertEqual([r for r in repo.list_active_upcoming_rows() if r[1] == self.CHAT], [])
        self.assertEqual(
            [r[1] for r in repo.list_active_upcoming_rows() if r[1] == self.OTHER_CHAT],
            [self.OTHER_CHAT],
            "отписка чата не гасит чужую подписку",
        )
        repo.upsert_chat_kind(self.CHAT, "upcoming_games")
        self.assertEqual(
            [r for r in repo.list_active_upcoming_rows() if r[1] == self.CHAT],
            [(upcoming, self.CHAT)],
            "переподписка — та же строка, отметки матчей сохраняются",
        )
        with closing(_connect()) as conn, conn:
            with self.assertRaises(psycopg2.errors.UniqueViolation), conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_subscriptions (chat_id, kind) VALUES (%s, 'upcoming_games')",
                    (self.CHAT,),
                )

    def test_country_constraints(self):
        """CHECK и уникальность миграции 0006: страна — ровно у ``country_players``."""
        self.repo.upsert_country(self.CHAT, "RUS")
        for error, statement in (
            (psycopg2.errors.CheckViolation,
             "INSERT INTO bot_subscriptions (chat_id, kind, team_id, country) "
             "VALUES (%s, 'team_scores', 7, 'RUS')"),
            (psycopg2.errors.UniqueViolation,
             "INSERT INTO bot_subscriptions (chat_id, kind, country) "
             "VALUES (%s, 'country_players', 'RUS')"),
        ):
            with self.subTest(statement=statement), closing(_connect()) as conn, conn:
                with self.assertRaises(error), conn.cursor() as cur:
                    cur.execute(statement, (self.CHAT,))


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestSubscriptionsMenu(unittest.IsolatedAsyncioTestCase):
    """Меню `/subscriptions` (Задача 63) на настоящей ``bot_subscriptions``: нажатия
    кнопок меняют строки таблицы, сводка и ✅ рисуются по ним, повторное нажатие не
    плодит дубли. Сезон и команды вымышлены (сезон 99999999), рейтинг стран подменён."""

    CHAT = -999000070
    SEASON = 99999999
    TEAMS = ((900011, "AAA", "Metropolitan"), (900012, "BBB", "Metropolitan"), (900013, "CCC", "Atlantic"))

    def setUp(self):
        from unittest import mock

        import bot
        import subscription_repo

        self.bot = bot
        self.repo = subscription_repo
        self._cleanup()
        self.addCleanup(self._cleanup)
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            for team_id, abbrev, division in self.TEAMS:
                cur.execute(
                    "INSERT INTO teams (team_id, season_id, abbreviation, division_name) "
                    "VALUES (%s, %s, %s, %s)",
                    (team_id, self.SEASON, abbrev, division),
                )
        for patcher in (
            mock.patch.object(config, "SEASON_ID", self.SEASON),
            mock.patch.object(bot, "country_rankings", lambda: ("", ["FIN", "RUS"])),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _cleanup(self):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("DELETE FROM bot_subscriptions WHERE chat_id = %s", (self.CHAT,))
            cur.execute("DELETE FROM teams WHERE season_id = %s", (self.SEASON,))

    def _rows(self):
        """Все строки чата, включая погашенные: (kind, team_id, country, active)."""
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, team_id, country, active FROM bot_subscriptions "
                "WHERE chat_id = %s ORDER BY kind, team_id, country",
                (self.CHAT,),
            )
            return cur.fetchall()

    def _fake_bot(self):
        """aiogram ``Bot`` на записывающей сессии (``tests/_telegram_fakes.py``) — без сети."""
        from aiogram import Bot

        from tests._telegram_fakes import FAKE_TOKEN, FakeSession

        return Bot(FAKE_TOKEN, session=FakeSession())

    async def _press(self, data):
        """Нажатие кнопки `data`: (подсказка, HTML экрана, [(подпись, callback_data)])."""
        from aiogram.types import CallbackQuery

        from tests._telegram_fakes import body

        fake_bot = self._fake_bot()
        callback = CallbackQuery.model_validate(
            {
                "id": "1",
                "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": 900, "date": datetime.now(timezone.utc),
                    "chat": {"id": self.CHAT, "type": "private"}, "text": "меню",
                },
            },
            context={"bot": fake_bot},
        )
        await self.bot.callback_subscriptions(callback)
        [edit] = fake_bot.session.edited_texts
        buttons = [] if edit.reply_markup is None else [
            (b.text, b.callback_data) for row in edit.reply_markup.inline_keyboard for b in row
        ]
        return fake_bot.session.answers[0], body(edit), buttons

    async def test_open_menu_on_empty_chat(self):
        from aiogram.types import Message

        from tests._telegram_fakes import body

        fake_bot = self._fake_bot()
        message = Message.model_validate(
            {
                "message_id": 1, "date": datetime.now(timezone.utc),
                "chat": {"id": self.CHAT, "type": "private"}, "text": "/subscriptions",
            },
            context={"bot": fake_bot},
        )
        await self.bot.cmd_subscriptions(message, fake_bot)
        self.assertEqual(fake_bot.session.sent_messages, [], "меню — rich-сообщение (Задача 91)")
        [reply] = fake_bot.session.sent_rich
        text, markup = body(reply), reply.reply_markup
        self.assertTrue(text.startswith("<p><b>Ваши подписки</b><br>Пока ничего нет.</p>"))
        # Когда приходят рассылки — служебное, подвал.
        self.assertTrue(text.endswith(
            "<br>Превью матчей дня — с 15:30 МСК, по одному раз в полчаса.</footer>"
        ))
        # Переключатели — каждый в своём ряду: вдвоём на телефоне не влезают.
        self.assertEqual([len(row) for row in markup.inline_keyboard], [1, 1, 2, 1])
        self.assertEqual(
            [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row],
            [("➕ Дайджест", "sub:d"), ("➕ Предстоящие матчи", "sub:u"),
             ("🏒 Команды (0)", "sub:T"), ("🌍 Страны (0)", "sub:C"), ("Готово", "sub:x")],
        )
        self.assertEqual(self._rows(), [])

    async def test_digest_toggle_writes_row_and_never_duplicates(self):
        hint, text, buttons = await self._press("sub:d")
        self.assertEqual(hint, "Дайджест включён")
        self.assertIn("<ul><li>Утренний дайджест</li></ul>", text)
        self.assertEqual(buttons[0], ("✅ Дайджест", "sub:d"))
        self.assertEqual(self._rows(), [("morning_digest", None, None, True)])

        hint, text, buttons = await self._press("sub:d")
        self.assertEqual(hint, "Дайджест выключен")
        self.assertIn("Пока ничего нет.", text)
        self.assertEqual(buttons[0], ("➕ Дайджест", "sub:d"))
        self.assertEqual(self._rows(), [("morning_digest", None, None, False)])

        await self._press("sub:d")
        self.assertEqual(self._rows(), [("morning_digest", None, None, True)], "одна строка")

    async def test_upcoming_toggle_writes_row_and_summary_line(self):
        """Задача 78: «Предстоящие матчи» — переключатель как у дайджеста, строка сводки
        «Предстоящие матчи» только у включённой подписки; дайджест не задевает."""
        self.assertRegex("sub:u", self.bot.SUBSCRIPTIONS_CALLBACK_PATTERN)
        hint, text, buttons = await self._press("sub:u")
        self.assertEqual(hint, "Предстоящие матчи включены")
        self.assertIn("<li>Предстоящие матчи</li>", text)
        self.assertNotIn("Утренний дайджест", text)
        self.assertEqual(buttons[:2], [("➕ Дайджест", "sub:d"), ("✅ Предстоящие матчи", "sub:u")])
        self.assertEqual(self._rows(), [("upcoming_games", None, None, True)])

        await self._press("sub:d")
        hint, text, buttons = await self._press("sub:u")
        self.assertEqual(hint, "Предстоящие матчи выключены")
        self.assertNotIn("<li>Предстоящие матчи</li>", text)
        self.assertIn("<li>Утренний дайджест</li>", text)
        self.assertEqual(buttons[1], ("➕ Предстоящие матчи", "sub:u"))
        self.assertEqual(
            self._rows(),
            [("morning_digest", None, None, True), ("upcoming_games", None, None, False)],
        )

        await self._press("sub:u")
        self.assertEqual(self._rows()[1], ("upcoming_games", None, None, True), "одна строка")

    async def test_team_toggle_by_division_updates_summary_and_counts(self):
        _, _, buttons = await self._press("sub:T")
        self.assertEqual(
            buttons,
            [("Atlantic", "sub:D:0"), ("Metropolitan", "sub:D:1"), ("Central", "sub:D:2"),
             ("Pacific", "sub:D:3"), ("« Назад", "sub:m")],
        )
        _, text, buttons = await self._press("sub:D:1")
        self.assertEqual(
            text,
            "<p><b>Metropolitan</b></p>"
            "<footer>Нажмите на команду, чтобы подписаться или отписаться.</footer>",
        )
        self.assertEqual(
            buttons,
            [("AAA", "sub:t:1:900011"), ("BBB", "sub:t:1:900012"), ("« К дивизионам", "sub:T")],
        )

        hint, _, buttons = await self._press("sub:t:1:900012")
        self.assertEqual(hint, "Подписка на BBB включена")
        self.assertEqual(buttons[:2], [("AAA", "sub:t:1:900011"), ("✅ BBB", "sub:t:1:900012")])
        self.assertEqual(self._rows(), [("team_scores", 900012, None, True)])

        _, _, buttons = await self._press("sub:T")
        self.assertIn(("Metropolitan (1)", "sub:D:1"), buttons)
        _, text, buttons = await self._press("sub:m")
        self.assertIn("<li>Команды: BBB</li>", text)
        self.assertIn(("🏒 Команды (1)", "sub:T"), buttons)

        hint, _, buttons = await self._press("sub:t:1:900012")
        self.assertEqual(hint, "Подписка на BBB отключена")
        self.assertEqual(buttons[1], ("BBB", "sub:t:1:900012"))
        self.assertEqual(self._rows(), [("team_scores", 900012, None, False)])

    async def test_country_toggle_and_done_removes_keyboard(self):
        _, _, buttons = await self._press("sub:C")
        self.assertEqual(buttons, [("FIN", "sub:c:FIN"), ("RUS", "sub:c:RUS"), ("« Назад", "sub:m")])

        hint, _, buttons = await self._press("sub:c:RUS")
        self.assertEqual(hint, "Подписка на RUS включена")
        self.assertEqual(buttons[:2], [("FIN", "sub:c:FIN"), ("✅ RUS", "sub:c:RUS")])
        await self._press("sub:c:FIN")
        await self._press("sub:c:FIN")
        self.assertEqual(
            self._rows(),
            [("country_players", None, "FIN", False), ("country_players", None, "RUS", True)],
        )

        _, text, buttons = await self._press("sub:x")
        self.assertIn("<li>Страны: RUS</li>", text)
        self.assertEqual(buttons, [], "«Готово» снимает клавиатуру")

    async def test_subscribed_country_outside_ranking_can_be_switched_off(self):
        self.repo.upsert_country(self.CHAT, "ZZZ")
        _, _, buttons = await self._press("sub:C")
        self.assertEqual(
            buttons,
            [("FIN", "sub:c:FIN"), ("RUS", "sub:c:RUS"), ("✅ ZZZ", "sub:c:ZZZ"), ("« Назад", "sub:m")],
        )
        hint, _, buttons = await self._press("sub:c:ZZZ")
        self.assertEqual(hint, "Подписка на ZZZ отключена")
        self.assertNotIn("ZZZ", " ".join(text for text, _ in buttons))
        self.assertEqual(self._rows(), [("country_players", None, "ZZZ", False)])

    async def test_failed_write_shows_error_and_no_checkmark(self):
        from unittest import mock

        with mock.patch.object(self.repo, "upsert_chat_kind", side_effect=psycopg2.Error("boom")):
            with self.assertLogs(self.bot.logger, level="ERROR"):
                _, text, buttons = await self._press("sub:d")
        self.assertEqual(text, f"<p>{self.bot._SUBSCRIPTIONS_DB_ERROR}</p>")
        self.assertEqual(buttons, [])
        self.assertEqual(self._rows(), [])


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestCountryNightSql(unittest.TestCase):
    """Запросы подписки на страну (Задача 61) на синтетической ночи: сезон, команды,
    игроки и страна ``ZZZ`` вымышлены, дата в 2099 году. Строки удаляются до и после."""

    SEASON = 99999999
    NIGHT = date(2099, 1, 2)
    # В int: карточка матча (`get_game_stats(integer)`) — часть сообщения (Задача 75).
    GAME = 2099990001
    GAME2 = 2099990002
    TEAM = 900001
    OPPONENT = 900002

    def setUp(self):
        import bot_messages

        self.messages = bot_messages
        # Шаблон карточки (`messages/game_message.txt`) читается от каталога бота.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(TELEGRAM_BOT)
        self._cleanup()
        self.addCleanup(self._cleanup)
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO teams (team_id, season_id, abbreviation, short_name) "
                "VALUES (%s, %s, 'HOM', 'Home'), (%s, %s, 'AWY', 'Away')",
                (self.TEAM, self.SEASON, self.OPPONENT, self.SEASON),
            )
            cur.execute(
                "INSERT INTO games (game_id, day, home_team_id, away_team_id, is_shootouts, "
                "season_id) VALUES (%s, %s, %s, %s, TRUE, %s), (%s, %s, %s, %s, FALSE, %s)",
                (self.GAME, self.NIGHT, self.TEAM, self.OPPONENT, self.SEASON,
                 self.GAME2, self.NIGHT, self.OPPONENT, self.TEAM, self.SEASON),
            )
            # Счёт и броски команд — из них карточка матча (`get_game_stats`).
            for game, home, away in (
                (self.GAME, self.TEAM, self.OPPONENT), (self.GAME2, self.OPPONENT, self.TEAM),
            ):
                cur.execute(
                    "INSERT INTO game_team_stats (game_id, team_id, field, goals, pim, shots, "
                    "blocked, hits) VALUES (%s, %s, 'home', 3, 2, 30, 5, 10), "
                    "(%s, %s, 'away', 2, 4, 25, 6, 12)",
                    (game, home, game, away),
                )
            # (player_id, фамилия, позиция, страна)
            for player in (
                (-1, "Скорер<", "C", "ZZZ"), (-2, "Тень", "L", "ZZZ"), (-3, "Чужой", "C", "YYY"),
                (-4, "Стенка", "G", "ZZZ"), (-5, "Запасной", "G", "ZZZ"),
                (-6, "Крыло", "R", "ZZZ"), (-7, "Защитник", "D", "ZZZ"),
                (-8, "Гость", "L", "ZZZ"),
            ):
                cur.execute(
                    "INSERT INTO rosters (player_id, season_id, lastname, position, nationality) "
                    "VALUES (%s, %s, %s, %s, %s)", (player[0], self.SEASON, *player[1:]),
                )
            # (игрок, голы, передачи, броски, +/-, хиты, блоки, вбрасывания выигр./всего,
            # время на льду): C, нулевой TOI, чужой, R, D.
            for stat in (
                (-1, 2, 0, 5, 1, 1, 0, 7, 12, "20:10"), (-2, 0, 0, 2, 0, 0, 0, 0, 0, "00:00"),
                (-3, 3, 0, 3, 2, 0, 0, 0, 0, "15:00"), (-6, 0, 1, 2, -1, 3, 4, None, None, "15:30"),
                (-7, 0, 0, 1, 0, 6, 5, None, None, "22:00"),
            ):
                cur.execute(
                    "INSERT INTO game_player_stats (player_id, team_id, game_id, goals, assists, "
                    "shots, plus_minus, hits, blocked, face_off_wins, face_off_taken, time_on_ice) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (stat[0], self.TEAM, self.GAME, *stat[1:]),
                )
            # Второй матч ночи: игрок ZZZ за соперника, без голов.
            cur.execute(
                "INSERT INTO game_player_stats (player_id, team_id, game_id, goals, assists, "
                "shots, plus_minus, hits, blocked, time_on_ice) "
                "VALUES (-8, %s, %s, 0, 0, 1, 0, 2, 0, '12:00')",
                (self.OPPONENT, self.GAME2),
            )
            for goalie in ((-4, "59:47", 30, 33, 90.91), (-5, "00:00", 0, 0, None)):
                cur.execute(
                    "INSERT INTO game_goalie_stats (player_id, team_id, game_id, timeonice, "
                    "saves, shots, save_percentage) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (goalie[0], self.TEAM, self.GAME, *goalie[1:]),
                )
            # (автор, период, время, event_id): два гола ZZZ, чужой, буллит ZZZ, овертайм ZZZ.
            for goal in (
                (-1, 1, "10:00", 10), (-1, 1, "02:00", 5), (-3, 2, "01:00", 20),
                (-1, 5, "00:00", 99), (-6, 4, "03:15", 30),
            ):
                cur.execute(
                    "INSERT INTO all_goals (goal_player_id, team_id, game_id, period, time, "
                    "event_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (goal[0], self.TEAM, self.GAME, *goal[1:]),
                )

    def _cleanup(self):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            games = ([self.GAME, self.GAME2],)
            for table in ("all_goals", "game_goalie_stats", "game_player_stats", "game_team_stats"):
                cur.execute(f"DELETE FROM {table} WHERE game_id = ANY(%s)", games)
            cur.execute("DELETE FROM games WHERE game_id = ANY(%s)", games)
            cur.execute("DELETE FROM rosters WHERE season_id = %s", (self.SEASON,))
            cur.execute("DELETE FROM teams WHERE season_id = %s", (self.SEASON,))

    def test_country_blocks_by_game_by_position_and_empty_without_players(self):
        """Задача 76: блок страны — по матчу, за ночь и для одного матча (карточка).
        Задача 64: у C — хиты и вбрасывания, у R — хиты, у D — блоки вместо хитов,
        у G — сейвы; пометка позиции только «ц»/«защ»; между игроками пустая строка."""
        block_game = "\n".join([
            "<b>ZZZ</b>",
            "",
            "Скорер&lt; (HOM, ц)",
            "2+0 · +1 · 5 бр. · 1 хит · вбр. 7/12 · 20:10",
            "",
            "Крыло (HOM)",
            "0+1 · −1 · 2 бр. · 3 хита · 15:30",
            "",
            "Защитник (HOM, защ)",
            "0+0 · +0 · 1 бр. · 5 блоков · 22:00",
            "",
            "Стенка (HOM)",
            "30/33 · 90.91% · 59:47",
        ])
        block_game2 = "<b>ZZZ</b>\n\nГость (AWY)\n0+0 · +0 · 1 бр. · 2 хита · 12:00"
        self.assertEqual(
            self.messages.country_night_blocks("ZZZ", self.NIGHT),
            [(self.GAME, block_game), (self.GAME2, block_game2)],
        )
        self.assertEqual(self.messages.country_game_block("ZZZ", self.GAME2), block_game2)
        self.assertEqual(self.messages.country_night_blocks("XXX", self.NIGHT), [])
        self.assertIsNone(self.messages.country_game_block("XXX", self.GAME))

    def test_score_line_is_the_first_line_of_the_card(self):
        """Короткое сообщение подписки начинается той же строкой счёта, что и карточка."""
        line = self.messages.game_score_line(self.GAME)
        self.assertEqual(line, "🏒 <b>Home 3:2 Away</b>")
        self.assertEqual(self.messages.game_message(self.GAME)[0].split("\n")[0], line)

    def test_night_goals_skip_shootout_in_game_order_with_scorer_country(self):
        self.assertEqual(
            self.messages.night_goals(self.NIGHT),
            [(self.GAME, 5, "ZZZ"), (self.GAME, 10, "ZZZ"), (self.GAME, 20, "YYY"), (self.GAME, 30, "ZZZ")],
        )

    def test_subscribed_games_match_teams_and_countries_of_each_chat(self):
        """Задача 85: матч ночи — чату, у которого на него подписка на команду (хозяева
        первыми) или страну, чьи игроки играли; погашенная подписка и чат без таких матчей
        в ответ не попадают."""
        import push_digest_job
        import subscription_repo as repo

        chats = (-999000085, -999000086, -999000087)
        self.addCleanup(self._delete_subscriptions, chats)
        both, yyy, gone = chats
        repo.upsert_team_scores(both, self.TEAM)
        repo.upsert_country(both, "ZZZ")
        repo.upsert_country(both, "XXX")
        repo.upsert_country(yyy, "YYY")
        repo.upsert_team_scores(gone, self.OPPONENT)
        repo.deactivate_chat(gone)
        with patch.object(config, "SEASON_ID", self.SEASON):
            subscribed = {
                chat: games
                for chat, games in push_digest_job.subscribed_games(self.NIGHT).items()
                if chat in chats
            }

        blocks = dict(self.messages.country_night_blocks("ZZZ", self.NIGHT))
        self.assertEqual(subscribed[both], {
            self.GAME: (["HOM"], [("ZZZ", blocks[self.GAME])]),
            self.GAME2: (["HOM"], [("ZZZ", blocks[self.GAME2])]),
        })
        self.assertEqual(
            {game: (subs.teams, [code for code, _ in subs.countries]) for game, subs in subscribed[yyy].items()},
            {self.GAME: ([], ["YYY"])},
        )
        self.assertNotIn(gone, subscribed)

    def _delete_subscriptions(self, chats):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("DELETE FROM bot_subscriptions WHERE chat_id = ANY(%s)", (list(chats),))


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestPredictionLogSql(unittest.TestCase):
    """Журнал прогнозов (Задача 77): настоящая ``publish_predictions`` на настоящем PG.

    Задача ``task_77_test`` вымышлена, поэтому публикация не трогает боевые строки
    ``game_predictions``; сезон, команды и игры — вымышленные, строки удаляются до и после.
    """

    TASK = "task_77_test"
    RUN_ID = "run_77"
    SEASON = 99999977
    HOME = 900071
    AWAY = 900072
    PLAYED = 2099990071
    UNPLAYED = 2099990072
    OLD_DAY = date(2000, 1, 1)

    def setUp(self):
        self._cleanup()
        self.addCleanup(self._cleanup)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        base = self.tmp / "artifacts" / "models" / self.TASK / "m"
        final = base / self.RUN_ID / "final"
        final.mkdir(parents=True)
        (final / "metadata.json").write_text(
            json.dumps({"run_id": self.RUN_ID, "status": "ok"}), encoding="utf-8"
        )
        os.symlink(Path(self.RUN_ID) / "final", base / "latest")
        self.csv = self.tmp / "preds.csv"
        self.csv.write_text(
            f"game_id,probability\n{self.PLAYED},0.6\n{self.UNPLAYED},0.4\n", encoding="utf-8"
        )

    def _games(self):
        return [self.PLAYED, self.UNPLAYED]

    def _cleanup(self):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("DELETE FROM prediction_log WHERE game_id = ANY(%s)", (self._games(),))
            cur.execute("DELETE FROM game_predictions WHERE task = %s", (self.TASK,))
            cur.execute("DELETE FROM game_team_stats WHERE game_id = ANY(%s)", (self._games(),))
            cur.execute("DELETE FROM games WHERE game_id = ANY(%s)", (self._games(),))
            cur.execute("DELETE FROM teams WHERE season_id = %s", (self.SEASON,))

    def _publish(self):
        from modeling.publish_predictions import publish_predictions

        with closing(_connect()) as conn:
            return publish_predictions(
                conn,
                task=self.TASK,
                model="m",
                predictions_csv=self.csv,
                artifacts_root=self.tmp / "artifacts",
            )

    def _log(self):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "SELECT game_id, published_on, probability, home_goals, away_goals, settled_at, "
                "home_won FROM prediction_log WHERE game_id = ANY(%s) ORDER BY game_id, published_on",
                (self._games(),),
            )
            return cur.fetchall()

    def test_journal_dedups_keeps_history_and_settles_played_games(self):
        self.assertEqual(self._publish(), 2)
        self.csv.write_text(
            f"game_id,probability\n{self.PLAYED},0.7\n{self.UNPLAYED},0.3\n", encoding="utf-8"
        )
        self.assertEqual(self._publish(), 2)
        rows = self._log()
        # Повтор в тот же день не множит строки, но обновляет вероятность; исхода пока нет.
        self.assertEqual([(r[0], r[2], r[3], r[4], r[5]) for r in rows],
                         [(self.PLAYED, 0.7, None, None, None), (self.UNPLAYED, 0.3, None, None, None)])
        today = rows[0][1]

        # Старая строка с ручным исходом (9:9) — «уже рассчитанная».
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO prediction_log (game_id, task, model, run_id, probability, "
                "published_on, home_goals, away_goals, settled_at) "
                "VALUES (%s, %s, 'm', %s, 0.5, %s, 9, 9, now())",
                (self.PLAYED, self.TASK, self.RUN_ID, self.OLD_DAY),
            )
            # Игра сыграна: хозяева выиграли в овертайме, итог 4:3 (goals с +1), основное время 3:3.
            cur.execute(
                "INSERT INTO teams (team_id, season_id, abbreviation, short_name) "
                "VALUES (%s, %s, 'HOM', 'Home'), (%s, %s, 'AWY', 'Away')",
                (self.HOME, self.SEASON, self.AWAY, self.SEASON),
            )
            cur.execute(
                "INSERT INTO games (game_id, day, home_team_id, away_team_id, winner_id, "
                "is_overtime, season_id) VALUES (%s, %s, %s, %s, %s, TRUE, %s), "
                "(%s, %s, %s, %s, NULL, FALSE, %s)",
                (self.PLAYED, today, self.HOME, self.AWAY, self.HOME, self.SEASON,
                 self.UNPLAYED, today, self.HOME, self.AWAY, self.SEASON),
            )
            cur.execute(
                "INSERT INTO game_team_stats (game_id, team_id, field, goals, fst_period_goals, "
                "snd_period_goals, trd_period_goals) "
                "VALUES (%s, %s, 'home', 4, 1, 1, 1), (%s, %s, 'away', 3, 1, 1, 1)",
                (self.PLAYED, self.HOME, self.PLAYED, self.AWAY),
            )

        self._publish()
        rows = {(r[0], r[1]): r for r in self._log()}
        self.assertEqual(len(rows), 3)
        settled = rows[(self.PLAYED, today)]
        self.assertEqual((settled[3], settled[4], settled[6]), (3, 3, True))
        self.assertIsNotNone(settled[5])
        # Старая рассчитанная строка пережила публикацию и не перезаписана.
        old = rows[(self.PLAYED, self.OLD_DAY)]
        self.assertEqual((old[2], old[3], old[4]), (0.5, 9, 9))
        # Игра без победителя остаётся без исхода.
        unplayed = rows[(self.UNPLAYED, today)]
        self.assertEqual((unplayed[3], unplayed[4], unplayed[5], unplayed[6]), (None, None, None, None))


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestGoalClipCacheSql(unittest.TestCase):
    """Кэш клипов голов (Задача 87) на настоящей ``bot_goal_clips``: юнит-тесты рассылки и
    кнопки видео подменяют кэш словарём, здесь — сам SQL чтения и перезаписи. Матч —
    несуществующий ``game_id``; строки удаляются до и после."""

    GAME = -2026000087

    def setUp(self):
        import video_replay

        self.clips = video_replay
        self._cleanup()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("DELETE FROM bot_goal_clips WHERE game_id = %s", (self.GAME,))

    def test_saved_clip_is_read_by_goal_and_reupload_overwrites_it(self):
        clips = self.clips
        self.assertIsNone(clips.cached_goal_clip(self.GAME, 100))

        clips.save_goal_clip(self.GAME, 100, "fid-old", "<b>GOAL</b> 100")
        clips.save_goal_clip(self.GAME, 101, "fid-101", "<b>GOAL</b> 101")
        self.assertEqual(clips.cached_goal_clip(self.GAME, 100), ("fid-old", "<b>GOAL</b> 100"))
        self.assertIsNone(clips.cached_goal_clip(self.GAME, 102))

        # Протухший file_id: повторная загрузка перезаписывает строку, а не падает на ключе.
        clips.save_goal_clip(self.GAME, 100, "fid-new", "<b>GOAL</b> 100")
        self.assertEqual(clips.cached_goal_clip(self.GAME, 100), ("fid-new", "<b>GOAL</b> 100"))
        self.assertEqual(clips.cached_goal_clip(self.GAME, 101), ("fid-101", "<b>GOAL</b> 101"))


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestAiQuestionsSql(unittest.TestCase):
    """Суточный лимит и журнал свободного текста (Задача 81) на настоящих ``ai_questions`` и
    ``ai_existing_users``. Юнит-тесты подменяют ответ БД, здесь проверяется сам SQL: граница
    суток по МСК и лимит текущего пользователя. Чаты — отрицательные, как в
    ``TestSubscriptionRepoSql``; строки удаляются до и после."""

    NEW_CHAT = -999000081
    EXISTING_CHAT = -999000082

    def setUp(self):
        import ai_router

        self.ai = ai_router
        self._cleanup()
        self.addCleanup(self._cleanup)
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ai_existing_users (chat_id) VALUES (%s)", (self.EXISTING_CHAT,))

    def _cleanup(self):
        chats = [self.NEW_CHAT, self.EXISTING_CHAT]
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ai_questions WHERE chat_id = ANY(%s)", (chats,))
            cur.execute("DELETE FROM ai_stats_log WHERE chat_id = ANY(%s)", (chats,))
            cur.execute("DELETE FROM ai_existing_users WHERE chat_id = ANY(%s)", (chats,))

    def test_limit_counts_todays_questions_by_moscow_day(self):
        ai = self.ai
        self.assertEqual(ai.questions_left(self.NEW_CHAT), ai.DAILY_LIMIT_NEW)
        self.assertEqual(ai.questions_left(self.EXISTING_CHAT), ai.DAILY_LIMIT_EXISTING)

        ai._log_question(self.NEW_CHAT, "лидеры", "screen", 900, tool="leaders",
                         tool_input={"category": "goals"}, cost_usd=0.004)
        ai._log_question(self.NEW_CHAT, "погода", "error", 30000, error="APITimeoutError")
        ai._log_question(self.EXISTING_CHAT, "таблица", "text", 700, answer="Пока не умею")
        # Минута до полуночи по Москве — вчерашний вопрос, в сегодняшний лимит не входит.
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_questions (chat_id, question, outcome, latency_ms, asked_at) "
                "VALUES (%s, 'вчера', 'text', 1, (date_trunc('day', now() AT TIME ZONE "
                "'Europe/Moscow') - interval '1 minute') AT TIME ZONE 'Europe/Moscow')",
                (self.NEW_CHAT,),
            )

        # Сбой API лимит не списывает: из трёх строк NEW_CHAT в счёт — только сегодняшний экран.
        self.assertEqual(ai.questions_left(self.NEW_CHAT), ai.DAILY_LIMIT_NEW - 1)
        self.assertEqual(ai.questions_left(self.EXISTING_CHAT), ai.DAILY_LIMIT_EXISTING - 1)
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tool, tool_input, cost_usd FROM ai_questions "
                "WHERE chat_id = %s AND outcome = 'screen'",
                (self.NEW_CHAT,),
            )
            self.assertEqual(cur.fetchall(), [("leaders", {"category": "goals"}, Decimal("0.004000"))])

    def test_stats_questions_count_once_through_ai_stats_log(self):
        """Вопрос статистики (Задача 83) списывает одну единицу: строка `ai_stats_log`;
        выбор `stats_question` маршрутизатором и сбой в журнале 83 в счёт не идут."""
        ai = self.ai
        ai._log_question(self.NEW_CHAT, "голы", "screen", 900, tool="stats_question", tool_input={})
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            for outcome in ("answer", "error", "ceiling"):
                cur.execute(
                    "INSERT INTO ai_stats_log (chat_id, question, continuation, outcome, latency_ms) "
                    "VALUES (%s, 'голы', false, %s, 5)",
                    (self.NEW_CHAT, outcome),
                )
        self.assertEqual(ai.questions_left(self.NEW_CHAT), ai.DAILY_LIMIT_NEW - 2)


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestAiStatsWindow(unittest.TestCase):
    """Окно продолжения диалога (Задача 83) на настоящем `ai_stats_log`: условие времени — на каждой
    строке, ошибки и чужие чаты в историю не попадают. Чаты отрицательные; строки чистятся."""

    CHAT = -999000084
    OTHER = -999000085

    def setUp(self):
        import ai_stats

        self.ai = ai_stats
        self._cleanup()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ai_stats_log WHERE chat_id = ANY(%s)", ([self.CHAT, self.OTHER],))

    def _add(self, chat, question, outcome, minutes_ago, answer=None):
        with closing(_connect()) as conn, conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_stats_log (chat_id, question, continuation, outcome, answer, latency_ms, "
                "asked_at) VALUES (%s, %s, false, %s, %s, 1, now() - %s * interval '1 minute')",
                (chat, question, outcome, answer or f"ответ {question}", minutes_ago),
            )

    def _questions(self, chat):
        return [p["question"] for p in self.ai.recent_pairs(chat)]

    def test_window_applies_to_every_row_not_only_the_newest(self):
        self._add(self.CHAT, "давний", "answer", 180)
        self.assertEqual(self._questions(self.CHAT), [], "все строки вне окна")

        self._add(self.CHAT, "почти вышел", "answer", 29.5)
        self._add(self.OTHER, "чужой", "answer", 1)
        self.assertEqual(self._questions(self.CHAT), ["почти вышел"], "давняя пара за окном не тянется")

        self._add(self.CHAT, "сбой", "error", 2)
        self._add(self.CHAT, "уточнение", "clarify", 1, answer="Какой Хьюз?")
        pairs = self.ai.recent_pairs(self.CHAT)
        self.assertEqual([p["question"] for p in pairs], ["почти вышел", "уточнение"], "от старых к новым")
        self.assertEqual(pairs[-1]["outcome"], "clarify")
        self.assertEqual(pairs[-1]["answer"], "Какой Хьюз?")

    def test_window_closes_after_thirty_minutes_and_keeps_three_pairs(self):
        self._add(self.CHAT, "вышел", "answer", 30.5)
        self.assertEqual(self._questions(self.CHAT), [])
        for i, minutes in enumerate((20, 15, 10, 5)):
            self._add(self.CHAT, f"в{i}", "answer", minutes)
        self.assertEqual(self._questions(self.CHAT), ["в1", "в2", "в3"])


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_SCHEMA_TESTS", "").strip().lower() in ("1", "true", "yes"),
    "schema checks off (enable: make test-db, or export RUN_DB_SCHEMA_TESTS=1)",
)
class TestAiReaderRole(unittest.TestCase):
    """Роль `nhl_ai_reader` (Задача 83, миграция 0015) — граница безопасности для SQL, который
    пишет модель: читает только таблицы данных, не пишет и не создаёт, не видит таблиц бота."""

    @staticmethod
    def _reader():
        return psycopg2.connect(
            host=config.PG_HOST, port=config.PG_PORT, user=config.AI_PG_USER,
            database=config.PG_DATABASE,
        )

    def _error_code(self, sql: str, before: str = "") -> str:
        """pgcode ошибки, с которой роль встречает *sql* (опционально после *before*)."""
        with closing(self._reader()) as conn, conn.cursor() as cur:
            # Автокоммит: SET действует со следующей транзакции, а не с уже открытой.
            conn.autocommit = True
            if before:
                cur.execute(before)
            with self.assertRaises(psycopg2.Error) as raised:
                cur.execute(sql)
            return raised.exception.pgcode

    def test_reads_data_tables_with_role_defaults(self):
        import ai_stats

        with closing(self._reader()) as conn, conn.cursor() as cur:
            cur.execute("SHOW statement_timeout")
            self.assertEqual(cur.fetchone()[0], "5s")
            cur.execute("SHOW default_transaction_read_only")
            self.assertEqual(cur.fetchone()[0], "on")
            for table in ai_stats.AI_TABLES:
                cur.execute(f"SELECT count(*) FROM {table}")
                self.assertGreaterEqual(cur.fetchone()[0], 0)

    def test_grants_are_exactly_select_on_ai_tables(self):
        import ai_stats

        with closing(_connect()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'nhl_ai_reader' AND table_schema = 'public'"
            )
            grants = cur.fetchall()
        self.assertEqual({p for _, p in grants}, {"SELECT"})
        self.assertEqual({t for t, _ in grants}, set(ai_stats.AI_TABLES))

    def test_bot_tables_are_invisible(self):
        for table in ("bot_subscriptions", "bot_chat_game_sent", "ai_questions", "ai_stats_log",
                      "ai_existing_users", "prediction_log", "game_predictions", "schema_migrations"):
            self.assertEqual(self._error_code(f"SELECT 1 FROM {table}"), "42501", table)

    def test_cannot_write_or_create_even_with_read_only_switched_off(self):
        off = "SET default_transaction_read_only = off"
        for sql in ("INSERT INTO games (game_id, season_id) VALUES (-1, 20212022)",
                    "UPDATE games SET day = day", "DELETE FROM games",
                    "CREATE TABLE ai_reader_probe (x int)", "DROP TABLE games"):
            # Права роли держат сами, без read-only сессии...
            self.assertEqual(self._error_code(sql, off), "42501", sql)
            # ...а read-only по умолчанию — вторая линия защиты.
            self.assertIn(self._error_code(sql), ("25006", "42501"), sql)

    def test_cannot_switch_to_another_role(self):
        self.assertEqual(self._error_code("SET ROLE postgres"), "42501")

    def test_run_readonly_turns_denials_and_timeout_into_model_errors(self):
        import ai_stats

        denied = ai_stats.run_readonly("SELECT * FROM bot_subscriptions")
        self.assertIsNone(denied.rows)
        self.assertIn("permission denied", denied.error)
        slow = ai_stats.run_readonly("SELECT pg_sleep(30)")
        self.assertIn("statement timeout", slow.error)
        ok = ai_stats.run_readonly("SELECT game_id FROM games LIMIT 2;")
        self.assertIsNone(ok.error)
        self.assertEqual(ok.rows, 2)

    def test_run_readonly_rejects_multi_statement_attacks(self):
        """SQL модели — один SELECT: ни `SET`, ни `COMMIT`, ни запись не проходят, как бы их ни
        склеивали точкой с запятой (simple query выполнил бы все операторы подряд)."""
        import time

        import ai_stats

        def large_objects() -> int:
            with closing(_connect()) as conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_largeobject_metadata")
                return cur.fetchone()[0]

        before = large_objects()
        started = time.monotonic()
        attacks = (
            "SET statement_timeout = 0; SELECT pg_sleep(7)",
            "SELECT set_config('statement_timeout', '0', false); SELECT pg_sleep(7)",
            "SET transaction_read_only = off; CREATE TEMP TABLE probe (x int); "
            "INSERT INTO probe VALUES (1); SELECT * FROM probe",
            "SET transaction_read_only = off; SELECT lo_from_bytea(0, '\\x41'); COMMIT; SELECT 1",
            "SELECT lo_from_bytea(0, '\\x41')",
            "WITH ins AS (INSERT INTO games (game_id, season_id) VALUES (-1, 20212022) RETURNING 1) "
            "SELECT * FROM ins",
        )
        for sql in attacks:
            outcome = ai_stats.run_readonly(sql)
            self.assertIsNotNone(outcome.error, sql)
            self.assertIsNone(outcome.rows, sql)
        self.assertLess(time.monotonic() - started, 5, "таймаут не обойдён: pg_sleep(7) не выполнялся")
        self.assertEqual(large_objects(), before, "large object не создан")


@unittest.skipIf(psycopg2 is None, "psycopg2 not installed")
@unittest.skipUnless(
    os.environ.get("RUN_DB_DATA_TESTS", "").strip() in ("1", "true", "yes"),
    "set RUN_DB_DATA_TESTS=1 after loading data (e.g. make season-sync-month)",
)
class TestNhlLoadedData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.conn = _connect()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"PostgreSQL unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "conn") and cls.conn:
            cls.conn.close()

    def test_has_games(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM games")
            n = cur.fetchone()[0]
        self.assertGreater(n, 0, "No games in DB; run the loader for your date window")

    def test_games_config_season_id_present(self):
        # DB is multi-season by design (Task 13): don't require every row to match
        # config.SEASON_ID, just that the configured season is actually loaded.
        # (season_id being non-NULL is already a DDL constraint, not worth re-testing;
        # orphan season_id vs teams is covered by test_home_away_teams_exist_for_season's
        # own NOT EXISTS query below — not by the games->teams FK itself, which is
        # MATCH SIMPLE and lets a row with home/away/winner all NULL bypass it.)
        sid = config.SEASON_ID
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM games WHERE season_id = %s", (sid,))
            config_count = cur.fetchone()[0]
        self.assertGreater(
            config_count, 0, f"config.SEASON_ID={sid} must be represented in games"
        )

    def test_home_away_teams_exist_for_season(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM games g
                WHERE NOT EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = g.home_team_id AND t.season_id = g.season_id
                )
                """
            )
            nh = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM games g
                WHERE NOT EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = g.away_team_id AND t.season_id = g.season_id
                )
                """
            )
            na = cur.fetchone()[0]
        self.assertEqual(nh, 0, "orphan home_team_id vs teams")
        self.assertEqual(na, 0, "orphan away_team_id vs teams")

    def test_game_fact_tables_reference_games(self):
        with self.conn.cursor() as cur:
            for tbl in (
                "game_team_stats",
                "game_player_stats",
                "game_goalie_stats",
                "all_goals",
                "game_three_stars",
                "shot_attempts",
            ):
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM {tbl} x
                    WHERE NOT EXISTS (SELECT 1 FROM games g WHERE g.game_id = x.game_id)
                    """
                )
                n = cur.fetchone()[0]
                self.assertEqual(n, 0, f"orphan game_id in {tbl}")

    def test_final_goals_are_regulation_plus_one_for_ot_or_shootout(self):
        # Задача 67: the goal model's target is the three periods, and `goals` must be exactly
        # that plus one for the OT/shootout winner (is_overtime is also true for shootouts).
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), COUNT(*) FILTER (
                    WHERE hs.goals + aws.goals
                       <> hs.fst_period_goals + hs.snd_period_goals + hs.trd_period_goals
                        + aws.fst_period_goals + aws.snd_period_goals + aws.trd_period_goals
                        + CASE WHEN g.is_overtime THEN 1 ELSE 0 END
                       OR hs.fst_period_goals IS NULL OR aws.fst_period_goals IS NULL
                )
                FROM games g
                JOIN game_team_stats hs ON hs.game_id = g.game_id AND hs.team_id = g.home_team_id
                JOIN game_team_stats aws ON aws.game_id = g.game_id AND aws.team_id = g.away_team_id
                WHERE g.winner_id IS NOT NULL
                """
            )
            played, broken = cur.fetchone()
        self.assertGreater(played, 0)
        self.assertEqual(broken, 0, "sum of goals != regulation periods + is_overtime")

    def test_rosters_team_ids_exist(self):
        # current_team_id is legally NULL (player landing fallback doesn't know
        # the season's team — see docs/pipeline_nulls_and_explicit_null_tz.md
        # §2.7). The composite FK (MATCH SIMPLE) allows that; this check must
        # too, or it false-fails on correctly loaded data.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM rosters r
                WHERE r.current_team_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM teams t
                    WHERE t.team_id = r.current_team_id AND t.season_id = r.season_id
                )
                """
            )
            n = cur.fetchone()[0]
        self.assertEqual(n, 0, "rosters.current_team_id must exist in teams for same season_id")

    def test_season_stats_season_id_not_orphaned_and_config_season_present(self):
        # Multi-season DB (Task 13): each table may hold several seasons, so we
        # don't require a single season across the table. Instead: (1) no row's
        # season_id is orphaned vs teams — these tables reach teams only via
        # rosters, whose season_id has no DB-level FK to teams (current_team_id
        # FK is nullable, see test_rosters_team_ids_exist), so a bad/typo'd
        # season_id here is a real, DB-undetected corruption class; (2) the
        # configured season is actually represented.
        sid = config.SEASON_ID
        with self.conn.cursor() as cur:
            for tbl in (
                "players_season_stats",
                "goalies_season_stats",
                "players_advanced_stats",
                "players_shot_types",
            ):
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM {tbl} x
                    WHERE NOT EXISTS (SELECT 1 FROM teams t WHERE t.season_id = x.season_id)
                    """
                )
                orphans = cur.fetchone()[0]
                self.assertEqual(orphans, 0, f"{tbl}.season_id not present in teams")
                cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE season_id = %s", (sid,))
                config_count = cur.fetchone()[0]
                self.assertGreater(
                    config_count,
                    0,
                    f"config.SEASON_ID={sid} must be represented in {tbl}",
                )

    def test_teams_and_stats_row_counts_match(self):
        sid = config.SEASON_ID
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM teams WHERE season_id = %s", (sid,))
            nt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM teams_stats WHERE season_id = %s", (sid,))
            ns = cur.fetchone()[0]
        self.assertEqual(nt, ns, "teams and teams_stats should have same team count per season_id")
        self.assertGreaterEqual(nt, 1)


if __name__ == "__main__":
    unittest.main()
