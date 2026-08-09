import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main as main_module
from bot import task_registry
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler


class MainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        task_registry._CHAT_TASKS.clear()

    async def asyncTearDown(self):
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

    async def test_application_registers_error_handler_and_starts(self):
        application = SimpleNamespace(
            add_handler=MagicMock(),
            add_error_handler=MagicMock(),
            initialize=AsyncMock(),
            start=AsyncMock(),
            updater=SimpleNamespace(start_polling=AsyncMock()),
        )
        builder = MagicMock()
        builder.token.return_value = builder
        builder.connect_timeout.return_value = builder
        builder.read_timeout.return_value = builder
        builder.write_timeout.return_value = builder
        builder.media_write_timeout.return_value = builder
        builder.pool_timeout.return_value = builder
        builder.concurrent_updates.return_value = builder
        builder.build.return_value = application

        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)

        with (
            patch.object(
                main_module.Application,
                "builder",
                return_value=builder,
            ),
            patch.object(
                main_module.menu,
                "set_bot_menu",
                new_callable=AsyncMock,
            ) as set_bot_menu,
            patch.object(
                main_module,
                "close_mtproto_downloader",
                new_callable=AsyncMock,
            ) as close_mtproto,
            patch.object(main_module.asyncio, "Future", return_value=completed),
        ):
            await main_module.main()

        builder.token.assert_called_once_with(main_module.BOT_TOKEN)
        builder.concurrent_updates.assert_called_once_with(False)
        application.add_error_handler.assert_called_once_with(
            main_module.handle_application_error
        )
        application.initialize.assert_awaited_once()
        set_bot_menu.assert_awaited_once_with(application)
        application.start.assert_awaited_once()
        application.updater.start_polling.assert_awaited_once()
        close_mtproto.assert_awaited_once_with()

        registered_commands = {
            command
            for call in application.add_handler.call_args_list
            if isinstance(call.args[0], CommandHandler)
            for command in call.args[0].commands
        }
        self.assertIn("authorize", registered_commands)
        self.assertIn("unauthorize", registered_commands)
        self.assertIn("remove", registered_commands)

        album_observers = [
            call
            for call in application.add_handler.call_args_list
            if isinstance(call.args[0], MessageHandler)
            and call.args[0].callback is main_module.observe_pdf_upload
        ]
        self.assertEqual(len(album_observers), 1)
        self.assertEqual(album_observers[0].kwargs.get("group"), -1)

        conversation = next(
            call.args[0]
            for call in application.add_handler.call_args_list
            if isinstance(call.args[0], ConversationHandler)
        )
        conversation_commands = {
            command: handler.callback
            for handler in conversation.entry_points
            if isinstance(handler, CommandHandler)
            for command in handler.commands
        }
        self.assertEqual(
            conversation_commands,
            {
                "run": main_module.run_command,
                "stop": main_module.stop_command,
            },
        )
        self.assertEqual(len(conversation.fallbacks), 1)
        self.assertIsInstance(conversation.fallbacks[0], CommandHandler)
        self.assertEqual(conversation.fallbacks[0].commands, frozenset({"stop"}))
        self.assertIs(
            conversation.fallbacks[0].callback,
            main_module.stop_command,
        )
        password_handlers = conversation.states[main_module.AWAITING_SUDO_PASSWORD]
        self.assertEqual(len(password_handlers), 1)
        self.assertIs(password_handlers[0].callback, main_module.password_input)

        upload_handlers = [
            call.args[0]
            for call in application.add_handler.call_args_list
            if isinstance(call.args[0], MessageHandler)
            and call.args[0].callback is main_module.tracked_file_upload
        ]
        self.assertEqual(len(upload_handlers), 1)
        self.assertTrue(upload_handlers[0].block)

        standalone_commands = {
            command: handler
            for call in application.add_handler.call_args_list
            if isinstance((handler := call.args[0]), CommandHandler)
            for command in handler.commands
        }
        expected_tracked_callbacks = {
            "splitpdf": main_module.tracked_split_pdf,
            "printer": main_module.tracked_split_pdf,
            "get_local_ip": main_module.tracked_local_ip,
            "get_public_ip": main_module.tracked_public_ip,
            "get_system_info": main_module.tracked_system_info,
            "get_machine_specs": main_module.tracked_machine_specs,
            "get_system_usage": main_module.tracked_system_usage,
            "get_disk_usage": main_module.tracked_disk_usage,
            "monitor_system_usage": main_module.tracked_system_monitor,
        }
        for command, callback in expected_tracked_callbacks.items():
            with self.subTest(command=command):
                handler = standalone_commands[command]
                self.assertIs(handler.callback, callback)
                # The launcher itself is awaited in update order. It reserves
                # the detached task before Telegram can dispatch a queued /stop.
                self.assertTrue(handler.block)

        self.assertIs(
            standalone_commands["runfile"].callback,
            main_module.execute_on_file,
        )

    async def test_tracked_launcher_registers_worker_before_returning(self):
        started = asyncio.Event()

        async def callback(_update, _context):
            started.set()
            await asyncio.Event().wait()

        tasks = []

        def create_task(coroutine, *, update=None, name=None):
            task = asyncio.create_task(coroutine, name=name)
            tasks.append(task)
            return task

        user = SimpleNamespace(id=1, username="test")
        update = SimpleNamespace(
            message=SimpleNamespace(chat_id=100, from_user=user),
            effective_chat=SimpleNamespace(id=100),
            effective_user=user,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(create_task=create_task)
        )
        launcher = main_module._tracked_handler(callback, "Test work")

        await launcher(update, context)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            task_registry.tracked_tasks(update),
            {tasks[0]: "Test work"},
        )
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(task_registry.tracked_tasks(update), {})

    async def test_application_error_handler_logs_context_exception(self):
        error = RuntimeError("handler failed")
        context = SimpleNamespace(error=error)

        with patch.object(main_module.logger, "error") as log_error:
            await main_module.handle_application_error(None, context)

        log_error.assert_called_once_with(
            "Unhandled exception while processing a Telegram update",
            exc_info=error,
        )


if __name__ == "__main__":
    unittest.main()
