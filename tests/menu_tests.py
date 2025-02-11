from telegram import Update, Message, User, Chat, ReplyKeyboardMarkup
from telegram.ext import CallbackContext
from unittest.mock import MagicMock
import menu

def test_start():
    # Create a mock update and context
    user = User(id=1, first_name='Test', is_bot=False)
    chat = Chat(id=1, type='private')
    message = Message(message_id=1, date=None, chat=chat, text=None, from_user=user)
    update = Update(update_id=1, message=message)
    context = CallbackContext(dispatcher=None)

    # Mock the reply_text method
    update.message.reply_text = MagicMock()

    # Call the start function
    menu.start(update, context)

    # Assert reply_text was called with the correct parameters
    reply_keyboard = [['/run', '/getLocalIP', '/getPublicIP']]
    update.message.reply_text.assert_called_once_with(
        'Please choose a command:',
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )