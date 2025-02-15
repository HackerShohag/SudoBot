import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, CallbackQueryHandler
from bot.config import BOT_TOKEN
from bot.bot import run_command, password_input, AWAITING_SUDO_PASSWORD
from bot.keyboard import handle_command_from_keyboard
from bot import menu
from bot.utils import authorize_user, remove_user

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler('run', run_command),
                CommandHandler("get_local_ip", menu.get_local_ip),
                CommandHandler("get_public_ip", menu.get_public_ip),
                CommandHandler("get_system_info", menu.get_system_info),
                CommandHandler("get_machine_specs", menu.get_machine_specs),
                CommandHandler("get_system_usage", menu.get_system_usage),
                CommandHandler("get_disk_usage", menu.get_disk_usage),
                CommandHandler("monitor_system_usage", menu.monitor_system_usage),
            ],
            states={AWAITING_SUDO_PASSWORD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, password_input)]},
            fallbacks=[],
        )
    )

    application.add_handler(CallbackQueryHandler(handle_command_from_keyboard))
    application.add_handler(CommandHandler("authorize", authorize_user))
    application.add_handler(CommandHandler("remove", remove_user))

    await application.initialize()
    print("Bot is running...")
    await menu.set_bot_menu(application)
    await application.start()
    await application.updater.start_polling()
    await asyncio.Future()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if 'This event loop is already running' in str(e):
            asyncio.ensure_future(main())
        else:
            raise
