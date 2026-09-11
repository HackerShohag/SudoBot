import asyncio
from functools import wraps
import logging
import os
from pathlib import Path
import re
import secrets
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
    FAILBACK_TIMEOUT,
    FAILOVER_TIMEOUT,
    HA_HEARTBEAT_FILE,
    HA_TAKEOVER_ACK_FILE,
    HA_TAKEOVER_REQUEST_FILE,
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
from bot.file_delivery import get_file
from bot.task_registry import cancel_all_tasks, register_task


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TAKEOVER_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


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
tracked_get_file = _tracked_handler(get_file, "file upload")
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
    application.add_handler(CommandHandler("get", tracked_get_file))
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


async def _run_server_ssh(remote_command: str, timeout: float = 20):
    """Run one internal SSH operation and return code/stdout/stderr."""
    try:
        process = await asyncio.create_subprocess_exec(
            *server_ssh_args(),
            remote_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return None, b"", str(exc).encode()

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except TimeoutError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        return None, b"", b"SSH command timed out"
    return process.returncode, stdout, stderr


async def _send_primary_heartbeat() -> bool:
    remote_command = f"touch -- {shlex.quote(HA_HEARTBEAT_FILE)}"
    return_code, _stdout, stderr = await _run_server_ssh(
        remote_command,
        timeout=min(HEARTBEAT_INTERVAL, 20),
    )
    if return_code == 0:
        return True
    detail = stderr.decode(errors="replace").strip()
    logger.warning("Server heartbeat failed: %s", detail[-500:])
    return False


async def send_primary_heartbeats() -> None:
    """Touch a server-side lease file over SSH every configured interval."""
    while True:
        await _send_primary_heartbeat()
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def _read_takeover_request() -> str | None:
    try:
        path = Path(HA_TAKEOVER_REQUEST_FILE)
        if path.stat().st_size > 128:
            return None
        nonce = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return nonce if TAKEOVER_NONCE_RE.fullmatch(nonce) else None


def _acknowledge_takeover(nonce: str) -> None:
    """Atomically acknowledge only after this server has stopped polling."""
    ack_path = Path(HA_TAKEOVER_ACK_FILE)
    ack_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ack_path.with_name(f".{ack_path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{nonce}\n", encoding="ascii")
    os.chmod(temporary, 0o600)
    os.replace(temporary, ack_path)


def _fresh_heartbeat_revision() -> int | None:
    """Return the revision of a current primary lease, if one exists."""
    try:
        stat = os.stat(HA_HEARTBEAT_FILE)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not inspect heartbeat file: %s", exc)
        return None

    age = time.time() - stat.st_mtime
    if -5 <= age <= FAILOVER_TIMEOUT:
        return stat.st_mtime_ns
    return None


async def wait_for_takeover_request(
    ignored_nonce: str | None,
) -> str | None:
    """Wait while active for a takeover request or a resumed heartbeat."""
    heartbeat_revision = _fresh_heartbeat_revision()
    while True:
        nonce = _read_takeover_request()
        if nonce is not None and nonce != ignored_nonce:
            return nonce

        current_revision = _fresh_heartbeat_revision()
        if (
            current_revision is not None
            and current_revision != heartbeat_revision
        ):
            # The local instance can remain alive through a temporary SSH
            # outage. Once its lease updates resume, local has priority and
            # the promoted server must immediately stop polling Telegram.
            logger.warning(
                "Primary heartbeat resumed; demoting active server"
            )
            return None
        await asyncio.sleep(0.5)


async def request_server_demotion() -> bool:
    """Ask a reachable promoted server to stop before local starts polling."""
    nonce = secrets.token_hex(16)
    request_path = shlex.quote(HA_TAKEOVER_REQUEST_FILE)
    temporary_path = shlex.quote(f"{HA_TAKEOVER_REQUEST_FILE}.{nonce}.tmp")
    heartbeat_path = shlex.quote(HA_HEARTBEAT_FILE)
    quoted_nonce = shlex.quote(nonce)
    remote_command = (
        "umask 077; "
        f"printf '%s\\n' {quoted_nonce} > {temporary_path} && "
        f"mv -- {temporary_path} {request_path} && "
        f"touch -- {heartbeat_path}"
    )
    return_code, _stdout, stderr = await _run_server_ssh(remote_command)
    if return_code != 0:
        detail = stderr.decode(errors="replace").strip()
        logger.critical(
            "VPS is unreachable, so local will start without a demotion "
            "acknowledgment (network partitions can cause split-brain): %s",
            detail[-500:],
        )
        return False

    ack_path = shlex.quote(HA_TAKEOVER_ACK_FILE)
    read_ack = f"if [ -r {ack_path} ]; then cat -- {ack_path}; fi"
    deadline = asyncio.get_running_loop().time() + FAILBACK_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        return_code, stdout, _stderr = await _run_server_ssh(
            read_ack,
            timeout=min(10, FAILBACK_TIMEOUT),
        )
        if return_code == 0 and secrets.compare_digest(
            stdout.decode(errors="replace").strip(),
            nonce,
        ):
            logger.warning("Server acknowledged demotion; local is taking over")
            return True
        await asyncio.sleep(1)

    raise RuntimeError(
        "The VPS accepted the takeover request but did not acknowledge "
        f"demotion within {FAILBACK_TIMEOUT:g} seconds; local will not poll "
        "BOT_TOKEN to avoid a conflict"
    )


async def run_main_bot(*, send_heartbeats: bool, stop_signal=None):
    """Run the sole Telegram poller using BOT_TOKEN."""
    application = build_main_application()
    heartbeat_task = None
    initialized = False
    stop_result = None
    stopped_by_signal = False

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
        if stop_signal is None:
            await asyncio.Future()
        else:
            stop_result = await stop_signal
            stopped_by_signal = True
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if stopped_by_signal:
            cancelled = cancel_all_tasks(exclude=asyncio.current_task())
            if cancelled:
                await asyncio.gather(
                    *(task for task, _label in cancelled),
                    return_exceptions=True,
                )
        if initialized:
            await _stop_application(application)
        await close_mtproto_downloader()
    return stop_result


async def wait_for_primary_timeout() -> str | None:
    """Watch the local lease file without contacting the Telegram API."""
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    last_mtime_ns = None
    last_request = None

    print(
        f"Server standby is monitoring {HA_HEARTBEAT_FILE}; "
        f"timeout is {FAILOVER_TIMEOUT:g}s...",
        flush=True,
    )
    while True:
        request = _read_takeover_request()
        if request is not None and request != last_request:
            # Standby is already demoted, so it can acknowledge immediately.
            _acknowledge_takeover(request)
            last_request = request

        # Only accept a newly touched, reasonably current file. This keeps a
        # stale lease left by an old primary from extending startup.
        heartbeat_revision = _fresh_heartbeat_revision()
        if (
            heartbeat_revision is not None
            and heartbeat_revision != last_mtime_ns
        ):
            last_mtime_ns = heartbeat_revision
            last_heartbeat = loop.time()
            logger.info("Primary heartbeat observed")

        if loop.time() - last_heartbeat >= FAILOVER_TIMEOUT:
            logger.critical(
                "No primary heartbeat for %.1f seconds; promoting server",
                FAILOVER_TIMEOUT,
            )
            return last_request
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
    if FAILBACK_TIMEOUT <= 0:
        raise RuntimeError("FAILBACK_TIMEOUT must be positive")
    if INSTANCE_ROLE == "local":
        # Resolve this at startup so a bad SSH configuration cannot silently
        # leave the server without heartbeats.
        server_ssh_args()


async def main():
    validate_config()
    if INSTANCE_ROLE == "server":
        while True:
            previous_request = await wait_for_primary_timeout()
            print(
                "Primary lease expired; starting BOT_TOKEN on server...",
                flush=True,
            )
            takeover_task = asyncio.create_task(
                wait_for_takeover_request(previous_request),
                name="server-failback-watcher",
            )
            try:
                nonce = await run_main_bot(
                    send_heartbeats=False,
                    stop_signal=takeover_task,
                )
            finally:
                if not takeover_task.done():
                    takeover_task.cancel()
                await asyncio.gather(takeover_task, return_exceptions=True)
            if nonce is not None:
                _acknowledge_takeover(nonce)
            print(
                "Local primary returned; server demoted to standby.",
                flush=True,
            )
    elif INSTANCE_ROLE == "local":
        await request_server_demotion()
        await run_main_bot(send_heartbeats=True)
    else:
        await run_main_bot(send_heartbeats=False)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if 'This event loop is already running' in str(e):
            asyncio.ensure_future(main())
        else:
            raise
