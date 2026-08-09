import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ConversationHandler

from bot.bot import (
    execute_command,
    execute_on_file,
    password_input,
    run_command,
    stop_command,
)
import bot.bot as command_bot
from bot import task_registry


def command_update(username="ordinary_user", *, chat_id=100):
    user = SimpleNamespace(id=200, username=username)
    message = SimpleNamespace(
        from_user=user,
        chat_id=chat_id,
        message_id=10,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=user,
    )


class HostCommandSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        command_bot._active_commands.clear()
        task_registry._CHAT_TASKS.clear()

    async def asyncTearDown(self):
        command_bot._active_commands.clear()
        pending = [
            task
            for tasks in task_registry._CHAT_TASKS.values()
            for task in tasks
            if not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        task_registry._CHAT_TASKS.clear()

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
        context = SimpleNamespace(args=["whoami"], user_data={}, chat_data={})

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await run_command(update, context)

        launch.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "❌ Only the super admin can run host commands."
        )

    async def test_runfile_rejects_non_super_admin_before_subprocess(self):
        update = command_update()
        context = SimpleNamespace(args=["file.txt", "cat", "{file}"])

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as create_process,
        ):
            await execute_on_file(update, context)

        create_process.assert_not_awaited()

    async def test_super_admin_run_reaches_guarded_executor(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace(args=["whoami"], user_data={}, chat_data={})

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await run_command(update, context)

        launch.assert_awaited_once_with("whoami", update, context, 10)

    async def test_super_admin_runfile_reaches_guarded_async_executor(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace(args=["file.txt", "cat", "{file}"])

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.os.path.exists", return_value=True),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await execute_on_file(update, context)

        launch.assert_awaited_once_with(
            "cat uploads/file.txt",
            update,
            context,
            10,
            subject="File command",
        )

    async def test_password_continuation_rejects_non_super_admin(self):
        update = command_update()
        context = SimpleNamespace(
            user_data={
                "command": "sudo whoami",
                "original_message_id": 9,
                "password_message_id": 8,
            },
            chat_data={
                "sudo_command": "sudo whoami",
                "sudo_original_message_id": 9,
                "sudo_password_message_id": 8,
            },
        )

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            result = await password_input(update, context)

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(context.chat_data, {})
        launch.assert_not_awaited()

    async def test_stop_rejects_non_super_admin(self):
        update = command_update()
        context = SimpleNamespace()
        process = SimpleNamespace(
            returncode=None,
            send_signal=MagicMock(),
        )
        active = command_bot._ActiveCommand(
            chat_key=("chat", 100),
            started_at=asyncio.get_running_loop().time(),
            process=process,
        )
        command_bot._active_commands[active.chat_key] = active
        tracked_task = asyncio.create_task(asyncio.Event().wait())
        task_registry.register_task(update, tracked_task, "PDF processing")

        with patch("bot.bot.is_super_admin", return_value=False):
            await stop_command(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "❌ Only the super admin can stop running tasks."
        )
        process.send_signal.assert_not_called()
        self.assertIs(command_bot._active_commands[active.chat_key], active)
        self.assertFalse(tracked_task.done())
        self.assertEqual(
            task_registry.tracked_tasks(update),
            {tracked_task: "PDF processing"},
        )

        tracked_task.cancel()
        await asyncio.gather(tracked_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
