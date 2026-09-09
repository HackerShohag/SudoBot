import asyncio
from dataclasses import dataclass, field
import html
import logging
import re
import shlex
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler, CallbackContext
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from bot.keyboard import update_command_history, update_keyboard
from bot.utils import is_super_admin
from bot.config import INSTANCE_ROLE, MAX_CHARS
from bot.ha import server_command
from bot.task_registry import (
    cancel_chat_tasks,
    has_stopping_tasks,
    register_task,
    unregister_task,
)
import signal
import os

AWAITING_SUDO_PASSWORD = 1
UPLOAD_DIR = "uploads"

logger = logging.getLogger(__name__)

# Telegram permits at most 4,096 characters in a text message.  Keep a small
# character-count safety margin. Four seconds is frequent enough to make a
# command feel alive without approaching Telegram's per-chat edit limits.
TELEGRAM_SAFE_MESSAGE_LIMIT = 4000
STATUS_EDIT_INTERVAL = 4.0
OUTPUT_TAIL_CHARS = 32_768
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9_])(?:\.env(?:\.[a-z0-9_-]+)?|"
    r"/proc/(?:self|[0-9]+)/environ)(?![a-z0-9_])"
)
SENSITIVE_EXPANSION_RE = re.compile(
    r"(?i)\$(?:\{)?[a-z0-9_]*(?:token|secret|password|api_hash|private_key)"
    r"[a-z0-9_]*(?:\})?"
)
ENVIRONMENT_DUMP_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*)(?:sudo\s+)?(?:/usr/bin/)?"
    r"(?:env|printenv|compgen\s+-e)(?:\s|$)|"
    r"(?:^|[;&|]\s*)(?:export|declare)\s+-p(?:\s|$)|"
    r"(?:^|[;&|]\s*)set\s*(?:$|[;&|])"
)
COMMAND_ENV_ALLOWLIST = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SSH_AUTH_SOCK",
    "TERM",
    "TMPDIR",
    "USER",
    "XDG_RUNTIME_DIR",
}


def _command_security_error(command: str) -> str | None:
    """Return a user-safe reason when a command directly targets secrets."""
    if SENSITIVE_PATH_RE.search(command):
        return "access to protected environment files is not allowed"
    if SENSITIVE_EXPANSION_RE.search(command):
        return "expansion of sensitive environment variables is not allowed"
    if ENVIRONMENT_DUMP_RE.search(command):
        return "dumping the process environment is not allowed"
    return None


def _command_environment() -> dict[str, str]:
    """Pass only ordinary runtime variables, never the bot's loaded .env."""
    return {
        name: value
        for name, value in os.environ.items()
        if name in COMMAND_ENV_ALLOWLIST
    }


async def _reject_unsafe_command(update, command: str, reply_to_message_id) -> bool:
    reason = _command_security_error(command)
    if reason is None:
        return False
    await update.message.reply_text(
        f"🛡️ Command blocked by the security checkpoint: {reason}.",
        reply_to_message_id=reply_to_message_id,
    )
    return True


def _message_limit() -> int:
    try:
        configured_limit = int(MAX_CHARS)
    except (TypeError, ValueError):
        configured_limit = 4096
    return max(512, min(configured_limit, TELEGRAM_SAFE_MESSAGE_LIMIT))


def _safe_output(text: str) -> str:
    """Make arbitrary process output safe for a plain Telegram text message."""
    text = ANSI_ESCAPE_RE.sub("", text)
    return "".join(
        character
        if character in "\n\t" or ord(character) >= 32
        else "�"
        for character in text
    )


def _limit_message(text: str) -> str:
    limit = _message_limit()
    if len(text) <= limit:
        return text

    marker = "\n\n… message shortened …\n\n"
    head_size = min(320, (limit - len(marker)) // 3)
    tail_size = limit - head_size - len(marker)
    return f"{text[:head_size]}{marker}{text[-tail_size:]}"


def _retry_after_seconds(value) -> float:
    if hasattr(value, "total_seconds"):
        value = value.total_seconds()
    try:
        return max(float(value), 0.1)
    except (TypeError, ValueError):
        return 1.0


@dataclass
class _OutputBuffer:
    tail: str = ""
    total_chars: int = 0

    def append(self, value) -> None:
        if not value:
            return
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        else:
            value = str(value)
        value = _safe_output(value)
        self.total_chars += len(value)
        self.tail = (self.tail + value)[-OUTPUT_TAIL_CHARS:]


@dataclass
class _StatusMessage:
    message: object
    last_text: str


@dataclass
class _ActiveCommand:
    chat_key: tuple[str, object]
    started_at: float
    subject: str = "Command"
    task: asyncio.Task | None = None
    process: object | None = None
    status: _StatusMessage | None = None
    stdout: _OutputBuffer = field(default_factory=_OutputBuffer)
    stderr: _OutputBuffer = field(default_factory=_OutputBuffer)
    stop_requested: bool = False
    spawning: bool = False
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_active_commands: dict[
    tuple[str, object],
    dict[int, _ActiveCommand],
] = {}


def _command_chat_key(update) -> tuple[str, object]:
    """Return a stable key so host commands and /stop stay chat-scoped."""
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        message = getattr(update, "message", None)
        chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return ("chat", chat_id)

    user = getattr(update, "effective_user", None)
    if user is None:
        message = getattr(update, "message", None)
        user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None)
    return ("user", user_id if user_id is not None else id(update))


def _commands_for_chat(chat_key) -> list[_ActiveCommand]:
    return list(_active_commands.get(chat_key, {}).values())


def _remember_active_command(active: _ActiveCommand) -> None:
    _active_commands.setdefault(active.chat_key, {})[id(active)] = active


async def _send_status(update, text: str, reply_to_message_id) -> _StatusMessage:
    text = _limit_message(text)
    message = await update.message.reply_text(
        text,
        reply_to_message_id=reply_to_message_id,
        parse_mode=ParseMode.HTML,
    )
    return _StatusMessage(message=message, last_text=text)


async def _edit_status(status: _StatusMessage, text: str) -> bool:
    """Edit one status message, respecting flood control and transient failures."""
    text = _limit_message(text)
    if text == status.last_text:
        return True

    network_attempts = 0
    while True:
        try:
            await status.message.edit_text(text, parse_mode=ParseMode.HTML)
        except RetryAfter as exc:
            await asyncio.sleep(_retry_after_seconds(exc.retry_after))
            continue
        except BadRequest as exc:
            # A timed-out edit may have succeeded at Telegram.  Retrying it can
            # legitimately produce this response, which is equivalent to success.
            if "message is not modified" in str(exc).casefold():
                status.last_text = text
                return True
            logger.warning("Could not edit command status: %s", exc)
            return False
        except NetworkError as exc:
            if network_attempts < 1:
                network_attempts += 1
                await asyncio.sleep(1)
                continue
            logger.warning("Could not edit command status after retry: %s", exc)
            return False
        except TelegramError as exc:
            logger.warning("Could not edit command status: %s", exc)
            return False
        else:
            status.last_text = text
            return True


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _escaped_tail(text: str, budget: int) -> str:
    """Return the longest HTML-escaped suffix that fits *budget*."""
    if budget <= 0:
        return ""
    low, high = 0, len(text)
    while low < high:
        size = (low + high + 1) // 2
        if len(html.escape(text[-size:])) <= budget:
            low = size
        else:
            high = size - 1
    return html.escape(text[-low:]) if low else ""


def _render_stream(label: str, output: _OutputBuffer, limit: int) -> str:
    prefix = f"{label}:\n<pre>"
    suffix = "</pre>"
    available = max(1, limit - len(prefix) - len(suffix))

    if output.total_chars == 0:
        return f"{prefix}(no output){suffix}"

    value = output.tail.rstrip("\n") or "(whitespace only)"
    escaped = html.escape(value)
    if len(escaped) <= available and output.total_chars <= len(output.tail):
        return f"{prefix}{escaped}{suffix}"

    marker = "… earlier output omitted …\n"
    escaped_value = _escaped_tail(value, available - len(marker))
    return f"{prefix}{marker}{escaped_value}{suffix}"


def _render_streams(
    stdout: _OutputBuffer,
    stderr: _OutputBuffer,
    budget: int,
    *,
    include_empty: bool,
) -> str:
    # stdout is the primary result and remains visible even while empty. stderr
    # is noise in the common success case, so only reveal it once it has data.
    streams = [("stdout", stdout)]
    if stderr.total_chars:
        streams.append(("stderr", stderr))

    separator_size = 2 * (len(streams) - 1)
    per_stream = max(40, (budget - separator_size) // len(streams))
    rendered = [
        _render_stream(label, output, per_stream) for label, output in streams
    ]
    return "\n\n".join(rendered)


def _running_text(active: _ActiveCommand, elapsed: float, frame: int) -> str:
    subject = active.subject.casefold()
    if active.stop_requested:
        title = f"🛑 Stopping {subject}…"
    else:
        title = (
            f"{SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]} "
            f"Running {subject}…"
        )
    header = f"{title}\nElapsed: {_format_elapsed(elapsed)}"
    budget = _message_limit() - len(header) - 2
    output = _render_streams(
        active.stdout,
        active.stderr,
        budget,
        include_empty=True,
    )
    return _limit_message(f"{header}\n\n{output}")


def _final_text(
    stdout: _OutputBuffer,
    stderr: _OutputBuffer,
    return_code: int,
    elapsed: float,
    *,
    stopped: bool,
    subject: str = "Command",
) -> str:
    if stopped:
        title = f"🛑 {subject} stopped"
    elif return_code == 0:
        title = f"✅ {subject} completed"
    else:
        title = f"❌ {subject} failed"

    header = (
        f"{title}\n"
        f"Exit code: {return_code}\n"
        f"Elapsed: {_format_elapsed(elapsed)}\n"
        f"Captured: {stdout.total_chars} stdout / {stderr.total_chars} stderr chars"
    )
    budget = _message_limit() - len(header) - 2
    output = _render_streams(
        stdout,
        stderr,
        budget,
        include_empty=True,
    )
    return _limit_message(f"{header}\n\n{output}")


async def _drain_stream(stream, output: _OutputBuffer) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        output.append(chunk)


def _signal_process(process, process_signal) -> None:
    """Signal the command's process group, falling back to the shell process."""
    process_id = getattr(process, "pid", None)
    if process_id is not None:
        try:
            os.killpg(process_id, process_signal)
            return
        except ProcessLookupError:
            return
        except OSError:
            # The fallback also keeps light-weight process doubles usable in tests.
            pass
    process.send_signal(process_signal)


async def _terminate_process(process) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        _signal_process(process, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            _signal_process(process, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()


async def _terminate_active_command(active: _ActiveCommand) -> None:
    """Terminate one command once, even if /stop races the runner."""
    async with active.termination_lock:
        await _terminate_process(active.process)


def _forget_active_command(active: _ActiveCommand) -> None:
    commands = _active_commands.get(active.chat_key)
    if commands is None:
        return
    commands.pop(id(active), None)
    if not commands:
        _active_commands.pop(active.chat_key, None)


async def _execute_active_command(
    active: _ActiveCommand,
    command: str,
    update,
    context,
    reply_to_message_id,
) -> None:
    """Own one reserved host command until its process and status are final."""
    loop = asyncio.get_running_loop()
    process = None
    wait_task = None
    drain_tasks = []

    try:
        # /stop can arrive immediately after /run, before this task gets its
        # first event-loop turn. In that case no child should be spawned.
        if active.stop_requested:
            return

        active.status = await _send_status(
            update,
            _running_text(active, 0, 0),
            reply_to_message_id,
        )

        if active.stop_requested:
            await _edit_status(
                active.status,
                _final_text(
                    active.stdout,
                    active.stderr,
                    -signal.SIGTERM,
                    loop.time() - active.started_at,
                    stopped=True,
                    subject=active.subject,
                ),
            )
            return

        active.spawning = True
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=_command_environment(),
            )
        finally:
            active.spawning = False
        active.process = process

        # Close the narrow race where /stop arrives while Python is awaiting
        # create_subprocess_shell and the process handle is not available yet.
        if active.stop_requested:
            await _terminate_active_command(active)

        try:
            update_command_history(update, context)
        except Exception as exc:
            # Command history is a convenience and must never prevent execution.
            logger.warning("Could not update command history: %s", exc)
        try:
            await update_keyboard(update, context)
        except Exception as exc:
            # Telegram or database trouble must not interrupt the host command.
            logger.warning("Could not update command keyboard: %s", exc)

        drain_tasks = [
            asyncio.create_task(_drain_stream(process.stdout, active.stdout)),
            asyncio.create_task(_drain_stream(process.stderr, active.stderr)),
        ]
        wait_task = asyncio.create_task(process.wait())
        frame = 0

        while not wait_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(wait_task),
                    timeout=STATUS_EDIT_INTERVAL,
                )
            except asyncio.TimeoutError:
                frame += 1
                elapsed = loop.time() - active.started_at
                await _edit_status(
                    active.status,
                    _running_text(active, elapsed, frame),
                )

        return_code = await wait_task
        drain_results = await asyncio.gather(*drain_tasks, return_exceptions=True)
        for stream_name, result in zip(("stdout", "stderr"), drain_results):
            if isinstance(result, Exception):
                active.stderr.append(f"\n[{stream_name} read failed: {result}]\n")

        elapsed = loop.time() - active.started_at
        await _edit_status(
            active.status,
            _final_text(
                active.stdout,
                active.stderr,
                return_code,
                elapsed,
                stopped=active.stop_requested,
                subject=active.subject,
            ),
        )
    except asyncio.CancelledError:
        active.stop_requested = True
        await _terminate_active_command(active)
        if active.status is not None:
            await _edit_status(
                active.status,
                _final_text(
                    active.stdout,
                    active.stderr,
                    -signal.SIGTERM,
                    loop.time() - active.started_at,
                    stopped=True,
                    subject=active.subject,
                ),
            )
        raise
    except Exception as exc:
        logger.exception("Command execution failed")
        await _terminate_active_command(active)
        error_output = _OutputBuffer()
        error_output.append(str(exc))
        if active.status is not None:
            await _edit_status(
                active.status,
                _final_text(
                    active.stdout,
                    error_output,
                    -1,
                    loop.time() - active.started_at,
                    stopped=active.stop_requested,
                    subject=active.subject,
                ),
            )
    finally:
        async def cleanup():
            if wait_task is not None and not wait_task.done():
                wait_task.cancel()
            for task in drain_tasks:
                if not task.done():
                    task.cancel()
            if wait_task is not None or drain_tasks:
                await asyncio.gather(
                    *([wait_task] if wait_task is not None else []),
                    *drain_tasks,
                    return_exceptions=True,
                )
            await _terminate_active_command(active)

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Cleanup is separately owned so a stop arriving during normal
            # finalization cannot strand a process or its output-drain tasks.
            await asyncio.shield(cleanup_task)
            raise
        finally:
            _forget_active_command(active)
            unregister_task(update, active.task)


async def _launch_command(
    command: str,
    update,
    context,
    reply_to_message_id,
    *,
    subject: str = "Command",
) -> bool:
    """Reserve and detach command work from Telegram dispatch."""
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return False
    if await _reject_unsafe_command(update, command, reply_to_message_id):
        return False

    chat_key = _command_chat_key(update)
    # Reserve before the first await/task switch. This makes an immediately
    # following /stop see the launch even before a subprocess handle exists.
    active = _ActiveCommand(
        chat_key=chat_key,
        started_at=asyncio.get_running_loop().time(),
        subject=subject,
    )
    _remember_active_command(active)
    coroutine = _execute_active_command(
        active,
        command,
        update,
        context,
        reply_to_message_id,
    )
    try:
        active.task = context.application.create_task(
            coroutine,
            update=update,
            name=(
                f"host-command:{chat_key[0]}:{chat_key[1]}:"
                f"{reply_to_message_id}"
            ),
        )
        active.task.add_done_callback(
            lambda _completed: _forget_active_command(active)
        )
        register_task(update, active.task, "Host command")
    except Exception:
        coroutine.close()
        _forget_active_command(active)
        logger.exception("Could not schedule host command")
        await update.message.reply_text(
            "❌ The host command could not be scheduled."
        )
        return False
    return True


async def execute_command(
    command: str,
    update,
    context,
    reply_to_message_id,
    *,
    subject: str = "Command",
):
    """Run and await a host command (kept for direct/internal callers)."""
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return
    if await _reject_unsafe_command(update, command, reply_to_message_id):
        return

    chat_key = _command_chat_key(update)
    active = _ActiveCommand(
        chat_key=chat_key,
        started_at=asyncio.get_running_loop().time(),
        subject=subject,
        task=asyncio.current_task(),
    )
    _remember_active_command(active)
    register_task(update, active.task, "Host command")
    await _execute_active_command(
        active,
        command,
        update,
        context,
        reply_to_message_id,
    )

async def execute_on_file(update: Update, context: CallbackContext) -> None:
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/runfile <filename> <command>`", parse_mode="MarkdownV2")
        return

    filename = context.args[0]  # Get filename
    command = " ".join(context.args[1:])  # Get the command  
    file_path = os.path.join(UPLOAD_DIR, filename)

    if not os.path.exists(file_path):
        await update.message.reply_text("❌ File not found.")
        return

    # Replace `{file}` with the actual file path
    command = command.replace("{file}", shlex.quote(file_path))

    await _launch_command(
        command,
        update,
        context,
        update.message.message_id,
        subject="File command",
    )

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return

    arguments = list(context.args)
    run_on_server = bool(arguments and arguments[0] == "--server")
    if run_on_server:
        arguments.pop(0)
    command = " ".join(arguments)

    if not command:
        # TODO: autofill the input field with the /run command so the user can just type the command
        usage = "/run [--server] <command>"
        await update.message.reply_text(
            f"Please provide a command to run. Usage: {usage}",
            reply_to_message_id=update.message.message_id,
        )
        return

    subject = "Command"
    if run_on_server:
        if INSTANCE_ROLE == "local":
            try:
                command = server_command(command)
            except RuntimeError as exc:
                await update.message.reply_text(
                    f"❌ Server routing is not configured: {exc}",
                    reply_to_message_id=update.message.message_id,
                )
                return
        elif INSTANCE_ROLE != "server":
            await update.message.reply_text(
                "❌ /run --server requires INSTANCE_ROLE=local or server.",
                reply_to_message_id=update.message.message_id,
            )
            return
        subject = "Server command"

    if command.startswith("sudo") and not (
        run_on_server and INSTANCE_ROLE == "local"
    ):
        if context.chat_data.get("sudo_command"):
            await update.message.reply_text(
                "⚠️ A sudo password prompt is already pending in this chat.",
                reply_to_message_id=update.message.message_id,
            )
            return
        message = await update.message.reply_text("Enter your sudo password:", reply_to_message_id=update.message.message_id)
        context.chat_data['sudo_command'] = command
        context.chat_data['sudo_original_message_id'] = update.message.message_id
        context.chat_data['sudo_password_message_id'] = message.message_id
        return AWAITING_SUDO_PASSWORD

    if run_on_server:
        await _launch_command(
            command,
            update,
            context,
            update.message.message_id,
            subject=subject,
        )
    else:
        await _launch_command(
            command,
            update,
            context,
            update.message.message_id,
        )

async def password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.message.from_user):
        context.chat_data.pop('sudo_command', None)
        context.chat_data.pop('sudo_original_message_id', None)
        context.chat_data.pop('sudo_password_message_id', None)
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return ConversationHandler.END

    password = update.message.text
    command = context.chat_data.get('sudo_command')
    original_message_id = context.chat_data.get('sudo_original_message_id')
    password_message_id = context.chat_data.get('sudo_password_message_id')

    if not command:
        await update.message.reply_text("Error: No command found.", reply_to_message_id=update.message.message_id)
        return ConversationHandler.END

    full_command = f"echo {password} | sudo -S {command[5:]}"

    await update.message.delete()
    await context.bot.delete_message(chat_id=update.message.chat_id, message_id=password_message_id)
    context.chat_data.pop('sudo_command', None)
    context.chat_data.pop('sudo_original_message_id', None)
    context.chat_data.pop('sudo_password_message_id', None)
    await _launch_command(
        full_command,
        update,
        context,
        original_message_id,
    )

    return ConversationHandler.END


async def _finish_stop_request(active: _ActiveCommand) -> None:
    """Finish terminating the selected process group without blocking updates."""
    try:
        await _terminate_active_command(active)
    except Exception:
        logger.exception("Could not finish stopping host command")


async def _mark_pending_pdf_prompt_stopped(context, chat_id, pending) -> None:
    message_id = pending.get("prompt_message_id") if pending else None
    if message_id is None:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="🛑 PDF upload request cancelled.",
        )
    except TelegramError:
        logger.warning("Could not mark the pending PDF prompt as stopped")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can stop running tasks."
        )
        return ConversationHandler.END

    # /stop is also the fallback for the sudo-password conversation and clears
    # a pending PDF re-upload prompt in this chat.
    had_pending_sudo = bool(context.chat_data.get('sudo_command'))
    pending_pdf = context.chat_data.get('pending_pdf_split')
    had_pending_pdf = bool(pending_pdf)
    context.chat_data.pop('sudo_command', None)
    context.chat_data.pop('sudo_original_message_id', None)
    context.chat_data.pop('sudo_password_message_id', None)
    context.chat_data.pop('pending_pdf_split', None)

    if had_pending_pdf:
        coroutine = _mark_pending_pdf_prompt_stopped(
            context,
            update.effective_chat.id,
            pending_pdf,
        )
        try:
            context.application.create_task(
                coroutine,
                update=update,
                name="stop-pending-pdf-prompt",
            )
        except Exception:
            coroutine.close()
            logger.warning("Could not schedule the pending PDF prompt update")

    active_commands = _commands_for_chat(_command_chat_key(update))
    host_stopping = []
    already_stopping = False
    excluded_tasks = {asyncio.current_task()}
    for active in active_commands:
        if (
            active.process is not None
            and active.process.returncode is not None
        ):
            continue
        if active.stop_requested:
            already_stopping = True
            continue

        # Mark all commands synchronously before yielding. Each runner owns its
        # own process group and final status message.
        active.stop_requested = True
        host_stopping.append(active)
        process = active.process
        if process is not None and process.returncode is None:
            try:
                _signal_process(process, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # Cancellation during create_subprocess_shell can lose a newly created
        # child before Python exposes its handle. Let that runner's post-spawn
        # stop check own its narrow phase.
        if active.spawning and active.task is not None:
            excluded_tasks.add(active.task)

    cancelled = cancel_chat_tasks(
        update,
        exclude=excluded_tasks,
    )
    cancelled_tasks = {task for task, _label in cancelled}

    # A host runner created by an internal caller may not be cancellable. Its
    # process was already signalled above, so finish the grace-period handling
    # without blocking Telegram update processing.
    for active in host_stopping:
        if active.task in cancelled_tasks or active.spawning:
            continue
        coroutine = _finish_stop_request(active)
        try:
            context.application.create_task(
                coroutine,
                update=update,
                name=(
                    "stop-host-command:"
                    f"{active.chat_key[0]}:{active.chat_key[1]}:{id(active)}"
                ),
            )
        except Exception:
            coroutine.close()
            logger.exception("Could not schedule host-command termination")
            await _terminate_active_command(active)

    stopped_count = (
        sum(active.task not in cancelled_tasks for active in host_stopping)
        + len(cancelled)
        + int(had_pending_sudo)
        + int(had_pending_pdf)
    )
    if stopped_count:
        noun = "task" if stopped_count == 1 else "tasks"
        await update.message.reply_text(
            f"🛑 Stopping all running work in this chat ({stopped_count} {noun})."
        )
    elif already_stopping or has_stopping_tasks(update):
        await update.message.reply_text(
            "🛑 Running work in this chat is already stopping."
        )
    else:
        await update.message.reply_text(
            "⚠️ No running tasks were found in this chat."
        )

    return ConversationHandler.END
