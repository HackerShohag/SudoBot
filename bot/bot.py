import asyncio
from dataclasses import dataclass, field
import logging
import re
import shlex
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CallbackContext
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from bot.keyboard import update_command_history
from bot.utils import is_super_admin
from bot.config import MAX_CHARS
import signal
import os

AWAITING_SUDO_PASSWORD = 1
running_process = None
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
    process: object
    status: _StatusMessage
    started_at: float
    subject: str = "Command"
    stdout: _OutputBuffer = field(default_factory=_OutputBuffer)
    stderr: _OutputBuffer = field(default_factory=_OutputBuffer)
    stop_requested: bool = False


_active_command = None
_command_starting = False


async def _send_status(update, text: str, reply_to_message_id) -> _StatusMessage:
    text = _limit_message(text)
    message = await update.message.reply_text(
        text,
        reply_to_message_id=reply_to_message_id,
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
            await status.message.edit_text(text)
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


def _render_stream(label: str, output: _OutputBuffer, limit: int) -> str:
    prefix = f"{label}:\n"
    available = max(1, limit - len(prefix))

    if output.total_chars == 0:
        return f"{prefix}(no output)"

    value = output.tail.rstrip("\n") or "(whitespace only)"
    if len(value) <= available and output.total_chars <= len(output.tail):
        return f"{prefix}{value}"

    marker = "… earlier output omitted …\n"
    tail_size = max(1, available - len(marker))
    return f"{prefix}{marker}{value[-tail_size:]}"


def _render_streams(
    stdout: _OutputBuffer,
    stderr: _OutputBuffer,
    budget: int,
    *,
    include_empty: bool,
) -> str:
    streams = [("stdout", stdout), ("stderr", stderr)]
    if not include_empty:
        streams = [(label, output) for label, output in streams if output.total_chars]
        if not streams:
            return "Waiting for output…"

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
        include_empty=False,
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

async def execute_command(
    command: str,
    update,
    context,
    reply_to_message_id,
    *,
    subject: str = "Command",
):
    global running_process, _active_command, _command_starting
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return

    if _command_starting or _active_command is not None or (
        running_process is not None and running_process.returncode is None
    ):
        await update.message.reply_text(
            "⚠️ Another host command is already running. Stop it with /stop first.",
            reply_to_message_id=reply_to_message_id,
        )
        return

    # Set this before the first await so two updates arriving together cannot
    # both pass the availability check and overwrite the global process handle.
    _command_starting = True
    status = None
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    process = None
    wait_task = None
    drain_tasks = []
    active = None

    try:
        status = await _send_status(
            update,
            (
                f"⠋ Running {subject.casefold()}…\n"
                "Elapsed: 0s\n\n"
                "Waiting for output…"
            ),
            reply_to_message_id,
        )
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        running_process = process
        active = _ActiveCommand(
            process=process,
            status=status,
            started_at=started_at,
            subject=subject,
        )
        _active_command = active
        _command_starting = False

        try:
            update_command_history(update, context)
        except Exception as exc:
            # Command history is a convenience and must never prevent execution.
            logger.warning("Could not update command history: %s", exc)

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
                elapsed = loop.time() - started_at
                await _edit_status(status, _running_text(active, elapsed, frame))

        return_code = await wait_task
        drain_results = await asyncio.gather(*drain_tasks, return_exceptions=True)
        for stream_name, result in zip(("stdout", "stderr"), drain_results):
            if isinstance(result, Exception):
                active.stderr.append(f"\n[{stream_name} read failed: {result}]\n")

        elapsed = loop.time() - started_at
        await _edit_status(
            status,
            _final_text(
                active.stdout,
                active.stderr,
                return_code,
                elapsed,
                stopped=active.stop_requested,
                subject=subject,
            ),
        )
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    except Exception as exc:
        logger.exception("Command execution failed")
        await _terminate_process(process)
        elapsed = loop.time() - started_at
        error_output = _OutputBuffer()
        error_output.append(str(exc))
        if status is not None:
            await _edit_status(
                status,
                _final_text(
                    _OutputBuffer(),
                    error_output,
                    -1,
                    elapsed,
                    stopped=False,
                    subject=subject,
                ),
            )
    finally:
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
        try:
            await _terminate_process(process)
        finally:
            # Never leave a stale global behind, even if process cleanup itself
            # encounters an unexpected platform-level error.
            if running_process is process:
                running_process = None
            if _active_command is active:
                _active_command = None
            _command_starting = False

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

    await execute_command(
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

    command = ' '.join(context.args)

    if not command:
        # TODO: autofill the input field with the /run command so the user can just type the command
        await update.message.reply_text("Please provide a command to run.", reply_to_message_id=update.message.message_id)
        return

    if command.startswith("sudo"):
        message = await update.message.reply_text("Enter your sudo password:", reply_to_message_id=update.message.message_id)
        context.user_data['command'] = command
        context.user_data['original_message_id'] = update.message.message_id
        context.user_data['password_message_id'] = message.message_id
        return AWAITING_SUDO_PASSWORD

    await execute_command(command, update, context, update.message.message_id)

async def password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.message.from_user):
        context.user_data.pop('command', None)
        context.user_data.pop('original_message_id', None)
        context.user_data.pop('password_message_id', None)
        await update.message.reply_text(
            "❌ Only the super admin can run host commands."
        )
        return ConversationHandler.END

    password = update.message.text
    command = context.user_data.get('command')
    original_message_id = context.user_data.get('original_message_id')
    password_message_id = context.user_data.get('password_message_id')

    if not command:
        await update.message.reply_text("Error: No command found.", reply_to_message_id=update.message.message_id)
        return ConversationHandler.END

    full_command = f"echo {password} | sudo -S {command[5:]}"
    
    await update.message.delete()
    await context.bot.delete_message(chat_id=update.message.chat_id, message_id=password_message_id)
    await execute_command(full_command, update, context, original_message_id)
    
    return ConversationHandler.END

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running_process, _active_command
    if not is_super_admin(update.message.from_user):
        await update.message.reply_text(
            "❌ Only the super admin can stop host commands."
        )
        return

    if running_process and running_process.returncode is None:
        process = running_process
        active = _active_command
        if active is not None and active.process is process:
            active.stop_requested = True
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
                pass
            else:
                await process.wait()
        # execute_command owns the status message and will turn it into the final
        # stopped result.  Avoid emitting a second completion message here.
    else:
        await update.message.reply_text("⚠️ No command is currently running.")
