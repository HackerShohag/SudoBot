import asyncio
from functools import wraps
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
from bot.task_registry import register_task


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _tracked_handler(callback, label):
    """Reserve a cancellable task before Telegram dequeues the next update."""
    @wraps(callback)
    async def launcher(update, context):
        coroutine = callback(update, context)
        try:
            task = context.application.create_task(
                coroutine,
                update=update,
                name=f"telegram-job:{label.casefold().replace(' ', '-')}",
            )
        except Exception:
            coroutine.close()
            raise
        register_task(update, task, label)

    return launcher


tracked_file_upload = _tracked_handler(handle_file_upload, "PDF processing")
tracked_split_pdf = _tracked_handler(split_pdf, "PDF processing")
tracked_local_ip = _tracked_handler(menu.get_local_ip, "local IP lookup")
tracked_public_ip = _tracked_handler(menu.get_public_ip, "public IP lookup")
tracked_system_info = _tracked_handler(menu.get_system_info, "system information")
tracked_machine_specs = _tracked_handler(menu.get_machine_specs, "machine specifications")
tracked_system_usage = _tracked_handler(menu.get_system_usage, "system usage")
tracked_disk_usage = _tracked_handler(menu.get_disk_usage, "disk usage")
tracked_system_monitor = _tracked_handler(
    menu.monitor_system_usage,
    "system monitor startup",
)


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
        # ConversationHandler requires ordered updates. Long-running work is
        # detached explicitly below instead of enabling global concurrency.
        .concurrent_updates(False)
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
                CommandHandler('stop', stop_command),
            ],
            states={AWAITING_SUDO_PASSWORD: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, password_input)]},
            # /stop must also cancel an unfinished sudo-password prompt.
            fallbacks=[CommandHandler('stop', stop_command)],
        )
    )

    # These handlers are not conversation states. PDF work may take minutes,
    # so PTB owns it as a non-blocking task while commands such as /stop and the
    # system monitors remain responsive.
    application.add_handler(
        MessageHandler(filters.Document.ALL, tracked_file_upload)
    )
    application.add_handler(CommandHandler("splitpdf", tracked_split_pdf))
    application.add_handler(CommandHandler("printer", tracked_split_pdf))
    application.add_handler(CommandHandler("runfile", execute_on_file))
    application.add_handler(
        CommandHandler("get_local_ip", tracked_local_ip)
    )
    application.add_handler(
        CommandHandler("get_public_ip", tracked_public_ip)
    )
    application.add_handler(
        CommandHandler("get_system_info", tracked_system_info)
    )
    application.add_handler(
        CommandHandler("get_machine_specs", tracked_machine_specs)
    )
    application.add_handler(
        CommandHandler("get_system_usage", tracked_system_usage)
    )
    application.add_handler(
        CommandHandler("get_disk_usage", tracked_disk_usage)
    )
    application.add_handler(
        CommandHandler(
            "monitor_system_usage",
            tracked_system_monitor,
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
