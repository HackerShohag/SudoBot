import asyncio
import logging

from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import BOT_TOKEN
from bot.bot import run_command, password_input, stop_command, AWAITING_SUDO_PASSWORD
from bot import menu
from bot.utils import (
    authorize_user,
    close_mtproto_downloader,
    handle_file_upload,
    observe_pdf_upload,
    remove_user,
    split_pdf,
)
from bot.bot import execute_on_file


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def handle_application_error(update, context):
    """Record unexpected handler failures with timestamps and tracebacks."""
    logger.error(
        "Unhandled exception while processing a Telegram update",
        exc_info=context.error,
    )

async def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .media_write_timeout(300)
        .pool_timeout(30)
        .build()
    )

    # Record album membership without downloading ordinary/unselected files.
    # A separate group keeps observation active during other conversations.
    application.add_handler(
        MessageHandler(filters.Document.ALL, observe_pdf_upload),
        group=-1,
    )

    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler('run', run_command),
                MessageHandler(filters.Document.ALL, handle_file_upload),
                CommandHandler("splitpdf", split_pdf),
                CommandHandler("printer", split_pdf),
                CommandHandler("runfile", execute_on_file),
                CommandHandler('stop', stop_command),
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

    application.add_handler(CommandHandler("authorize", authorize_user))
    # /remove remains a backwards-compatible alias for /unauthorize.
    application.add_handler(
        CommandHandler(("unauthorize", "remove"), remove_user)
    )
    application.add_error_handler(handle_application_error)

    try:
        await application.initialize()
        print("Bot is running...", flush=True)
        await menu.set_bot_menu(application)
        await application.start()
        await application.updater.start_polling()
        await asyncio.Future()
    finally:
        await close_mtproto_downloader()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if 'This event loop is already running' in str(e):
            asyncio.ensure_future(main())
        else:
            raise
