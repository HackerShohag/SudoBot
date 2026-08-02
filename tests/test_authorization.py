import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.utils import (
    authorize_user,
    init_db,
    is_admin,
    is_super_admin,
    is_user_authorized,
    remove_user,
)


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
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
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

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
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

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
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

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
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

    async def test_only_configured_admin_username_is_super_admin(self):
        self.admin.username = "HackerShohag"
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE authorized_users SET username = ? WHERE user_id = ?",
                (self.admin.username, self.admin.id),
            )
        other_admin = telegram_user(999, "another_admin", "Another Admin")
        self.insert_user(other_admin, "admin")

        with (
            patch("bot.utils.DB_PATH", self.db_path),
            patch("bot.utils.SUPER_ADMIN_USERNAME", "hackershohag"),
        ):
            self.assertTrue(is_super_admin(self.admin))
            self.assertFalse(is_super_admin(other_admin))

    async def test_configured_super_admin_bootstraps_without_database_row(self):
        owner = telegram_user(1000, "ConfiguredOwner", "Configured Owner")

        with (
            patch("bot.utils.DB_PATH", self.db_path),
            patch("bot.utils.SUPER_ADMIN_USERNAME", "configuredowner"),
        ):
            self.assertTrue(is_user_authorized(owner))
            self.assertTrue(is_admin(owner))
            self.assertTrue(is_super_admin(owner))

    async def test_admin_can_revoke_user_by_username(self):
        target = telegram_user(400, "TargetUser", "Target User")
        self.insert_user(target, "user")
        update = self.make_update()
        context = SimpleNamespace(args=["@targetuser"])

        with patch("bot.utils.DB_PATH", self.db_path):
            await remove_user(update, context)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM authorized_users WHERE user_id = ?",
                (target.id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        update.message.reply_text.assert_awaited_once_with(
            "✅ Access revoked for @targetuser."
        )

    async def test_admin_can_revoke_user_by_reply_without_username(self):
        target = telegram_user(401, None, "No Username")
        self.insert_user(target, "user")
        update = self.make_update(replied_user=target)
        context = SimpleNamespace(args=[])

        with patch("bot.utils.DB_PATH", self.db_path):
            await remove_user(update, context)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM authorized_users WHERE user_id = ?",
                (target.id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        update.message.reply_text.assert_awaited_once_with(
            "✅ Access revoked for ID 401."
        )

    async def test_non_admin_cannot_revoke_user(self):
        target = telegram_user(402, "protected_user", "Protected User")
        requester = telegram_user(403, "ordinary_user", "Ordinary User")
        self.insert_user(target, "user")
        self.insert_user(requester, "user")
        update = self.make_update()
        update.message.from_user = requester
        context = SimpleNamespace(args=[target.username])

        with patch("bot.utils.DB_PATH", self.db_path):
            await remove_user(update, context)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM authorized_users WHERE user_id = ?",
                (target.id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 1)
        update.message.reply_text.assert_awaited_once_with(
            "❌ You are not authorized to revoke users."
        )

    async def test_admin_cannot_revoke_configured_super_admin(self):
        owner = telegram_user(404, "HackerShohag", "Owner")
        self.insert_user(owner, "admin")
        update = self.make_update()
        context = SimpleNamespace(args=["@hackershohag"])

        with (
            patch("bot.utils.DB_PATH", self.db_path),
            patch("bot.utils.SUPER_ADMIN_USERNAME", "hackershohag"),
        ):
            await remove_user(update, context)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM authorized_users WHERE user_id = ?",
                (owner.id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 1)
        update.message.reply_text.assert_awaited_once_with(
            "❌ The configured super admin cannot be unauthorized."
        )

    async def test_init_db_creates_missing_parent_directory(self):
        nested_path = Path(self.temp_dir.name) / "new" / "db" / "users.db"

        with patch("bot.utils.DB_PATH", str(nested_path)):
            init_db()

        self.assertTrue(nested_path.is_file())


if __name__ == "__main__":
    unittest.main()
