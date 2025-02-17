import unittest
from bot.menu import *
from unittest.mock import AsyncMock, patch
from telegram import Update, User, Message
from bot.menu import get_machine_specs

class TestMenu(unittest.TestCase):
    def test_example(self):
        self.assertTrue(True)  # Replace with actual tests
        class TestMenu(unittest.TestCase):
            @patch('bot.menu.is_user_authorized', return_value=True)
            @patch('bot.menu.platform.processor', return_value='Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz')
            @patch('bot.menu.psutil.cpu_count', return_value=8)
            @patch('bot.menu.platform.system', return_value='Linux')
            @patch('builtins.open', new_callable=unittest.mock.mock_open, read_data='model name\t: Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz\ncpu MHz\t\t: 1992.000\nvendor_id\t: GenuineIntel')
            @patch('bot.menu.get_ram_info', return_value=[{'Manufacturer': 'Samsung', 'Speed': '2400 MHz', 'Type': 'DDR4', 'Form Factor': 'SODIMM', 'Size': '8 GB'}])
            @patch('bot.menu.GPUtil.getGPUs', return_value=[])
            def test_get_machine_specs(self, mock_is_user_authorized, mock_processor, mock_cpu_count, mock_system, mock_open, mock_get_ram_info, mock_getGPUs):
                update = Update(update_id=123, message=Message(message_id=1, date=None, chat=None, text=None, from_user=User(id=1, is_bot=False, first_name='Test', username='testuser')))
                context = AsyncMock()

                asyncio.run(get_machine_specs(update, context))

                context.bot.send_message.assert_called_once_with(
                    chat_id=update.effective_chat.id,
                    text=(
                        "CPU: Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz (Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz)\n"
                        "CPU Cores: 8\n"
                        "CPU Speed: 1992.000 MHz\n"
                        "CPU Vendor: GenuineIntel\n"
                        "RAM Info:\n1. Samsung 8 GB DDR4 SODIMM (2400 MHz)\n"
                        "GPU Info:\nN/A\n"
                    )
                )

            @patch('bot.menu.is_user_authorized', return_value=False)
            def test_get_machine_specs_unauthorized(self, mock_is_user_authorized):
                update = Update(update_id=123, message=Message(message_id=1, date=None, chat=None, text=None, from_user=User(id=1, is_bot=False, first_name='Test', username='testuser')))
                context = AsyncMock()

                asyncio.run(get_machine_specs(update, context))

                context.bot.send_message.assert_called_once_with(
                    chat_id=update.effective_chat.id,
                    text="❌ You are not authorized to run commands."
                )
if __name__ == "__main__":
    unittest.main()
