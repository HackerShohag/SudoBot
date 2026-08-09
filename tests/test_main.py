import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main as main_module
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler


class MainTests(unittest.IsolatedAsyncioTestCase):
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
        pdf_callbacks = {
            command: handler.callback
            for handler in conversation.entry_points
            if isinstance(handler, CommandHandler)
            for command in handler.commands
            if command in {"splitpdf", "printer"}
        }
        self.assertEqual(set(pdf_callbacks), {"splitpdf", "printer"})
        self.assertTrue(
            all(
                callback is main_module.split_pdf
                for callback in pdf_callbacks.values()
            )
        )

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
