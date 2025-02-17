import unittest
from bot.utils import *
from unittest.mock import AsyncMock, MagicMock, patch
from bot.utils import authorize_user
from bot.utils import authorize_user
from bot.utils import authorize_user
from bot.utils import authorize_user

class TestUtils(unittest.TestCase):
    def test_example(self):
        self.assertTrue(True)  # Replace with actual tests
        class TestUtils(unittest.TestCase):
            @patch("bot.utils.is_admin", return_value=True)
            @patch("bot.utils.sqlite3.connect")
            def test_authorize_user_success(self, mock_connect, mock_is_admin):
                mock_update = MagicMock()
                mock_context = MagicMock()
                mock_update.message.from_user.id = 123
                mock_update.message.from_user.username = "admin_user"
                mock_update.message.reply_to_message = None
                mock_context.args = ["new_user", "admin"]

                mock_conn = mock_connect.return_value
                mock_cursor = mock_conn.cursor.return_value


                authorize_user(mock_update, mock_context)

                mock_cursor.execute.assert_called_with(
                    "INSERT INTO authorized_users (username, user_id, full_name, added_by, role) VALUES (?, ?, ?, ?, ?)",
                    ("new_user", None, mock_update.message.from_user.full_name, "admin_user", "admin")
                )
                mock_conn.commit.assert_called_once()
                mock_conn.close.assert_called_once()

            @patch("bot.utils.is_admin", return_value=False)
            async def test_authorize_user_not_admin(self, mock_is_admin):
                mock_update = MagicMock()
                mock_context = MagicMock()
                mock_update.message.from_user.id = 123


                await authorize_user(mock_update, mock_context)

                mock_update.message.reply_text.assert_called_with("❌ You are not authorized to add users.")

            @patch("bot.utils.is_admin", return_value=True)
            async def test_authorize_user_invalid_role(self, mock_is_admin):
                mock_update = MagicMock()
                mock_context = MagicMock()
                mock_update.message.from_user.id = 123
                mock_update.message.from_user.username = "admin_user"
                mock_update.message.reply_to_message = None
                mock_context.args = ["new_user", "invalid_role"]


                await authorize_user(mock_update, mock_context)

                mock_update.message.reply_text.assert_called_with("❌ Invalid role. Use 'admin' or 'user'.")

            @patch("bot.utils.is_admin", return_value=True)
            @patch("bot.utils.sqlite3.connect")
            async def test_authorize_user_already_authorized(self, mock_connect, mock_is_admin):
                mock_update = MagicMock()
                mock_context = MagicMock()
                mock_update.message.from_user.id = 123
                mock_update.message.from_user.username = "admin_user"
                mock_update.message.reply_to_message = None
                mock_context.args = ["new_user", "admin"]

                mock_conn = mock_connect.return_value
                mock_cursor = mock_conn.cursor.return_value
                mock_cursor.execute.side_effect = sqlite3.IntegrityError


                await authorize_user(mock_update, mock_context)

                mock_update.message.reply_text.assert_called_with("❌ User is already authorized.")
                mock_conn.close.assert_called_once()

        if __name__ == "__main__":
            unittest.main()
if __name__ == "__main__":
    unittest.main()
