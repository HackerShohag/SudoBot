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
            patch("bot.bot.is_admin", return_value=False),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await run_command(update, context)

        launch.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "❌ Only authorized admins can run host commands."
        )

    async def test_admin_cannot_use_run_to_list_hidden_files(self):
        for command in (
            "ls -la",
            "ls .env",
            "ls --all",
            "find . -name '.*'",
            "cat .gitignore",
            "python -c 'print(1)'",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(
                    command_bot._admin_command_security_error(command)
                )

    async def test_admin_can_run_a_safe_read_only_command(self):
        update = command_update("admin_user")
        context = SimpleNamespace(
            args=["df", "-h"],
            user_data={},
            chat_data={},
        )

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot.is_admin", return_value=True),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await run_command(update, context)

        launch.assert_awaited_once_with(
            "df -h",
            update,
            context,
            10,
            allow_admin=True,
            policy_command="df -h",
        )

    async def test_internal_launcher_blocks_unsafe_admin_command(self):
        update = command_update("admin_user")
        context = SimpleNamespace(
            application=SimpleNamespace(create_task=MagicMock())
        )

        with (
            patch("bot.bot.is_super_admin", return_value=False),
            patch("bot.bot.is_admin", return_value=True),
        ):
            launched = await command_bot._launch_command(
                "cat README.md",
                update,
                context,
                10,
                allow_admin=True,
            )

        self.assertFalse(launched)
        context.application.create_task.assert_not_called()
        update.message.reply_text.assert_awaited_once()
        self.assertIn(
            "Admin command blocked",
            update.message.reply_text.await_args.args[0],
        )

    async def test_checkpoint_blocks_environment_file_access(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace()

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as create_process,
        ):
            await execute_command("cat .env", update, context, 10)

        create_process.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "🛡️ Command blocked by the security checkpoint: "
            "access to protected environment files is not allowed.",
            reply_to_message_id=10,
        )

    def test_checkpoint_covers_common_environment_leaks(self):
        blocked = (
            "cat /proc/self/environ",
            "echo $BOT_TOKEN",
            "printenv",
            "env | sort",
            "python -c \"print(open('.env').read())\"",
        )
        for command in blocked:
            with self.subTest(command=command):
                self.assertIsNotNone(
                    command_bot._command_security_error(command)
                )

    def test_child_environment_uses_a_small_allowlist(self):
        values = {
            "PATH": "/usr/bin",
            "HOME": "/home/bot",
            "BOT_TOKEN": "secret-token",
            "TELEGRAM_API_HASH": "secret-hash",
            "AWS_ACCESS_KEY_ID": "secret-cloud-key",
        }
        with patch.dict(command_bot.os.environ, values, clear=True):
            child_environment = command_bot._command_environment()

        self.assertEqual(
            child_environment,
            {"PATH": "/usr/bin", "HOME": "/home/bot"},
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

    async def test_server_flag_relays_from_local_instance(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace(
            args=["--server", "hostname"],
            user_data={},
            chat_data={},
        )

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch.object(command_bot, "INSTANCE_ROLE", "local"),
            patch.object(
                command_bot,
                "server_command",
                return_value="ssh-safe-command",
            ) as route,
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await run_command(update, context)

        route.assert_called_once_with("hostname")
        launch.assert_awaited_once_with(
            "ssh-safe-command",
            update,
            context,
            10,
            subject="Server command",
        )

    async def test_server_flag_executes_directly_after_server_takeover(self):
        update = command_update("HackerShohag")
        context = SimpleNamespace(
            args=["--server", "hostname"],
            user_data={},
            chat_data={},
        )

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch.object(command_bot, "INSTANCE_ROLE", "server"),
            patch("bot.bot._launch_command", new_callable=AsyncMock) as launch,
        ):
            await run_command(update, context)

        launch.assert_awaited_once_with(
            "hostname",
            update,
            context,
            10,
            subject="Server command",
        )

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
        command_bot._remember_active_command(active)
        tracked_task = asyncio.create_task(asyncio.Event().wait())
        task_registry.register_task(update, tracked_task, "PDF processing")

        with patch("bot.bot.is_super_admin", return_value=False):
            await stop_command(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "❌ Only the super admin can stop running tasks."
        )
        process.send_signal.assert_not_called()
        self.assertIn(active, command_bot._commands_for_chat(active.chat_key))
        self.assertFalse(tracked_task.done())
        self.assertEqual(
            task_registry.tracked_tasks(update),
            {tracked_task: "PDF processing"},
        )

        tracked_task.cancel()
        await asyncio.gather(tracked_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
