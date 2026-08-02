import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ConversationHandler

from bot.bot import (
    execute_command,
    execute_on_file,
    password_input,
    run_command,
    stop_command,
)


def command_update(username="ordinary_user"):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=200, username=username),
        message_id=10,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(message=message)


class HostCommandSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_executor_has_defense_in_depth_gate(self):
        update = command_update()
        context = SimpleNamespace()

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as create_process,
        ):
            await execute_command("whoami", update, context, 10)

        create_process.assert_not_awaited()

    async def test_run_rejects_non_super_admin_before_execution(self):
        update = command_update()
        context = SimpleNamespace(args=["whoami"], user_data={})

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot.execute_command", new_callable=AsyncMock) as execute,
        ):
            await run_command(update, context)

        execute.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "❌ Only the super admin can run host commands."
        )

    async def test_runfile_rejects_non_super_admin_before_subprocess(self):
        update = command_update()
        context = SimpleNamespace(args=["file.txt", "cat", "{file}"])

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot.subprocess.run") as subprocess_run,
        ):
            await execute_on_file(update, context)

        subprocess_run.assert_not_called()

    async def test_super_admin_run_reaches_guarded_executor(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace(args=["whoami"], user_data={})

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.execute_command", new_callable=AsyncMock) as execute,
        ):
            await run_command(update, context)

        execute.assert_awaited_once_with("whoami", update, context, 10)

    async def test_super_admin_runfile_reaches_subprocess(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace(args=["file.txt", "cat", "{file}"])
        result = SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.os.path.exists", return_value=True),
            patch("bot.bot.subprocess.run", return_value=result) as run,
        ):
            await execute_on_file(update, context)

        run.assert_called_once()

    async def test_password_continuation_rejects_non_super_admin(self):
        update = command_update()
        context = SimpleNamespace(
            user_data={
                "command": "sudo whoami",
                "original_message_id": 9,
                "password_message_id": 8,
            }
        )

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot.execute_command", new_callable=AsyncMock) as execute,
        ):
            result = await password_input(update, context)

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(context.user_data, {})
        execute.assert_not_awaited()

    async def test_stop_rejects_non_super_admin(self):
        update = command_update()
        context = SimpleNamespace()

        with patch("bot.bot.is_super_admin", return_value=False):
            await stop_command(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "❌ Only the super admin can stop host commands."
        )


if __name__ == "__main__":
    unittest.main()
