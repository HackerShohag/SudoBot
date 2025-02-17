import unittest
from bot.bot import *

class TestBot(unittest.TestCase):
    def test_example(self):
        self.assertTrue(True)  # Replace with actual tests
        class TestBot(unittest.TestCase):
            # Existing test_example method...

            @unittest.mock.patch('bot.bot.asyncio.create_subprocess_shell')
            @unittest.mock.patch('bot.bot.update_command_history')
            @unittest.mock.patch('bot.bot.update_keyboard')
            @unittest.mock.patch('bot.bot.ContextTypes.DEFAULT_TYPE.bot.send_message')
            @unittest.mock.patch('bot.bot.ContextTypes.DEFAULT_TYPE.bot.edit_message_text')
            async def test_execute_command(self, mock_edit_message_text, mock_send_message, mock_update_keyboard, mock_update_command_history, mock_create_subprocess_shell):
                mock_process = unittest.mock.AsyncMock()
                mock_process.stdout = unittest.mock.AsyncMock()
                mock_process.stderr = unittest.mock.AsyncMock()
                mock_create_subprocess_shell.return_value = mock_process

                mock_process.stdout.__aiter__.return_value = [b'line1\n', b'line2\n']
                mock_process.stderr.read.return_value = b''

                update = unittest.mock.Mock()
                context = unittest.mock.Mock()
                reply_to_message_id = 123

                await execute_command('echo test', update, context, reply_to_message_id)

                mock_update_command_history.assert_called_once_with(update, context)
                mock_send_message.assert_called_once()
                mock_edit_message_text.assert_called()
                mock_update_keyboard.assert_called_once_with(update, context)

            @unittest.mock.patch('bot.bot.asyncio.create_subprocess_shell')
            @unittest.mock.patch('bot.bot.update_command_history')
            @unittest.mock.patch('bot.bot.update_keyboard')
            @unittest.mock.patch('bot.bot.ContextTypes.DEFAULT_TYPE.bot.send_message')
            @unittest.mock.patch('bot.bot.ContextTypes.DEFAULT_TYPE.bot.edit_message_text')
            async def test_execute_command_with_error(self, mock_edit_message_text, mock_send_message, mock_update_keyboard, mock_update_command_history, mock_create_subprocess_shell):
                mock_process = unittest.mock.AsyncMock()
                mock_process.stdout = unittest.mock.AsyncMock()
                mock_process.stderr = unittest.mock.AsyncMock()
                mock_create_subprocess_shell.return_value = mock_process

                mock_process.stdout.__aiter__.return_value = [b'line1\n', b'line2\n']
                mock_process.stderr.read.return_value = b'error'

                update = unittest.mock.Mock()
                context = unittest.mock.Mock()
                reply_to_message_id = 123

                await execute_command('echo test', update, context, reply_to_message_id)

                mock_update_command_history.assert_called_once_with(update, context)
                mock_send_message.assert_called_once()
                mock_edit_message_text.assert_called()
                mock_update_keyboard.assert_called_once_with(update, context)
if __name__ == "__main__":
    unittest.main()
