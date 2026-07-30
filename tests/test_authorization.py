import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.utils import authorize_user, init_db, is_user_authorized


def telegram_user(user_id, username, full_name):
    return SimpleNamespace(
        id=user_id,
        username=username,
        full_name=full_name,
    )


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "authorized_users.db")
        with patch("bot.utils.DB_PATH", self.db_path):
            init_db()
        self.admin = telegram_user(100, "admin_user", "Admin User")
        self.insert_user(self.admin, "admin")

    def tearDown(self):
        self.temp_dir.cleanup()

    def insert_user(self, user, role, user_id=True):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO authorized_users
                    (username, user_id, full_name, added_by, role)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user.username,
                    user.id if user_id else None,
                    user.full_name,
                    self.admin.username if hasattr(self, "admin") else "setup",
                    role,
                ),
            )

    def make_update(self, replied_user=None):
        reply = (
            SimpleNamespace(from_user=replied_user)
            if replied_user is not None
            else None
        )
        message = SimpleNamespace(
            from_user=self.admin,
            reply_to_message=reply,
            reply_text=AsyncMock(),
        )
        return SimpleNamespace(message=message)

    async def test_pending_username_binds_on_first_use(self):
        pending = telegram_user(200, "PendingUser", "Pending User")
        self.insert_user(pending, "user", user_id=False)

        with patch("bot.utils.DB_PATH", self.db_path):
            self.assertTrue(
                is_user_authorized(
                    telegram_user(200, "pendinguser", "Pending User")
                )
            )

        with sqlite3.connect(self.db_path) as connection:
            bound_id = connection.execute(
                "SELECT user_id FROM authorized_users WHERE username = ?",
                ("PendingUser",),
            ).fetchone()[0]
        self.assertEqual(bound_id, 200)

    async def test_reply_authorization_updates_username_placeholder(self):
        target = telegram_user(300, "reply_user", "Reply User")
        self.insert_user(target, "user", user_id=False)
        update = self.make_update(replied_user=target)
        context = SimpleNamespace(args=[])

        with patch("bot.utils.DB_PATH", self.db_path):
            await authorize_user(update, context)

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT user_id, role FROM authorized_users
                WHERE username = ?
                """,
                ("reply_user",),
            ).fetchone()
        self.assertEqual(row, (300, "user"))
        update.message.reply_text.assert_awaited_once_with(
            "✅ User @reply_user updated as user."
        )

    async def test_username_authorization_explains_pending_id_link(self):
        update = self.make_update()
        context = SimpleNamespace(args=["@mentioned_user", "user"])

        with patch("bot.utils.DB_PATH", self.db_path):
            await authorize_user(update, context)

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT user_id, role FROM authorized_users
                WHERE username = ?
                """,
                ("mentioned_user",),
            ).fetchone()
        self.assertEqual(row, (None, "user"))
        response = update.message.reply_text.await_args.args[0]
        self.assertIn("ID will be linked", response)


if __name__ == "__main__":
    unittest.main()
