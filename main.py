import asyncio
from functools import wraps
import logging
import os
import shlex
import time

from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import (
    BOT_TOKEN,
    FAILOVER_TIMEOUT,
    HA_HEARTBEAT_FILE,
    HEARTBEAT_INTERVAL,
    INSTANCE_ROLE,
)
from bot.ha import server_ssh_args
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

def build_main_application():
    """Build the user-facing application without starting its lifecycle."""
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

    return application


async def _stop_application(application) -> None:
    if application.updater and getattr(application.updater, "running", False):
        await application.updater.stop()
    if getattr(application, "running", False):
        await application.stop()
    shutdown = getattr(application, "shutdown", None)
    if shutdown is not None:
        await shutdown()


async def send_primary_heartbeats() -> None:
    """Touch a server-side lease file over SSH every configured interval."""
    ssh_args = server_ssh_args()
    remote_command = f"touch -- {shlex.quote(HA_HEARTBEAT_FILE)}"
    while True:
        try:
            process = await asyncio.create_subprocess_exec(
                *ssh_args,
                remote_command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(HEARTBEAT_INTERVAL, 20),
                )
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            except TimeoutError:
                process.kill()
                await process.wait()
                logger.warning("Server heartbeat SSH command timed out")
            else:
                if process.returncode:
                    detail = stderr.decode(errors="replace").strip()
                    logger.warning(
                        "Server heartbeat failed (exit %s): %s",
                        process.returncode,
                        detail[-500:],
                    )
        except OSError as exc:
            logger.warning("Could not start heartbeat SSH command: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def run_main_bot(*, send_heartbeats: bool) -> None:
    """Run the sole Telegram poller using BOT_TOKEN."""
    application = build_main_application()
    heartbeat_task = None
    initialized = False

    try:
        await application.initialize()
        initialized = True
        await menu.set_bot_menu(application)
        await application.start()
        await application.updater.start_polling()
        if send_heartbeats:
            heartbeat_task = asyncio.create_task(
                send_primary_heartbeats(),
                name="primary-heartbeat",
            )
        print(f"Bot is running on {INSTANCE_ROLE}...", flush=True)
        await asyncio.Future()
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if initialized:
            await _stop_application(application)
        await close_mtproto_downloader()


async def wait_for_primary_timeout() -> None:
    """Watch the local lease file without contacting the Telegram API."""
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    last_mtime_ns = None

    print(
        f"Server standby is monitoring {HA_HEARTBEAT_FILE}; "
        f"timeout is {FAILOVER_TIMEOUT:g}s...",
        flush=True,
    )
    while True:
        try:
            stat = os.stat(HA_HEARTBEAT_FILE)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not inspect heartbeat file: %s", exc)
        else:
            # Only accept a newly touched, reasonably current file. This keeps
            # a stale lease left by an old primary from extending startup.
            age = time.time() - stat.st_mtime
            if stat.st_mtime_ns != last_mtime_ns and -5 <= age <= FAILOVER_TIMEOUT:
                last_mtime_ns = stat.st_mtime_ns
                last_heartbeat = loop.time()
                logger.info("Primary heartbeat observed")

        if loop.time() - last_heartbeat >= FAILOVER_TIMEOUT:
            logger.critical(
                "No primary heartbeat for %.1f seconds; promoting server",
                FAILOVER_TIMEOUT,
            )
            return
        await asyncio.sleep(1)


def validate_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if INSTANCE_ROLE not in {"standalone", "local", "server"}:
        raise RuntimeError("INSTANCE_ROLE must be standalone, local, or server")
    if HEARTBEAT_INTERVAL <= 0 or FAILOVER_TIMEOUT <= HEARTBEAT_INTERVAL:
        raise RuntimeError(
            "FAILOVER_TIMEOUT must be greater than the positive "
            "HEARTBEAT_INTERVAL"
        )
    if INSTANCE_ROLE == "local":
        # Resolve this at startup so a bad SSH configuration cannot silently
        # leave the server without heartbeats.
        server_ssh_args()


async def main():
    validate_config()
    if INSTANCE_ROLE == "server":
        await wait_for_primary_timeout()
        print("Primary lease expired; starting BOT_TOKEN on server...", flush=True)
        await run_main_bot(send_heartbeats=False)
    else:
        await run_main_bot(send_heartbeats=INSTANCE_ROLE == "local")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if 'This event loop is already running' in str(e):
            asyncio.ensure_future(main())
        else:
            raise
