import asyncio
import logging
import platform
import shutil
import socket

import GPUtil
import psutil
import requests
from telegram import BotCommand, Update
from telegram.error import NetworkError, RetryAfter
from telegram.ext import ContextTypes

from bot.utils import is_user_authorized
from bot.task_registry import (
    register_current_task,
    register_task,
    unregister_task,
)
from bot.status_animation import (
    STATUS_FRAMES,
    animate_status,
    animated_status_text,
    stop_animation,
)


LOGGER = logging.getLogger(__name__)
UPLOAD_DIR = "uploads"
GIB = 1024**3
PUBLIC_IP_TIMEOUT_SECONDS = 10
MONITOR_DURATION_SECONDS = 5 * 60
MONITOR_INTERVAL_SECONDS = 1.0
NETWORK_EDIT_RETRY_SECONDS = 1
_ACTIVE_MONITORS = {}


def _read_linux_cpu_info():
    """Return the first value for each useful field in /proc/cpuinfo."""
    details = {}
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as cpuinfo:
            for line in cpuinfo:
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                normalized_key = key.strip().lower()
                if normalized_key not in details:
                    details[normalized_key] = value.strip()
    except OSError:
        pass
    return details


def _collect_cpu_details():
    uname = platform.uname()
    linux_details = _read_linux_cpu_info() if platform.system() == "Linux" else {}

    platform_processor = (platform.processor() or uname.processor or "").strip()
    cpu_name = (
        linux_details.get("model name")
        or linux_details.get("hardware")
        or platform_processor
        or uname.machine
        or "Unknown"
    )
    cpu_vendor = (
        linux_details.get("vendor_id")
        or linux_details.get("cpu implementer")
        or "Unknown"
    )

    cpu_speed = linux_details.get("cpu mhz")
    try:
        frequency = psutil.cpu_freq()
        if frequency and frequency.current:
            cpu_speed = f"{frequency.current:.0f}"
    except (AttributeError, NotImplementedError, OSError):
        pass

    return {
        "name": cpu_name,
        "vendor": cpu_vendor,
        "speed_mhz": cpu_speed or "Unknown",
        "logical_cpus": psutil.cpu_count(logical=True) or "Unknown",
        "architecture": uname.machine or "Unknown",
    }


def _probe_nvidia_gpus():
    """GPUtil is NVIDIA-only; isolate its failures from all other metrics."""
    try:
        return GPUtil.getGPUs(), None
    except Exception as exc:
        error = " ".join(str(exc).split())
        if len(error) > 180:
            error = f"{error[:177]}..."
        return [], error or type(exc).__name__


def _format_gpu_usage():
    gpus, error = _probe_nvidia_gpus()
    if error:
        return f"GPU Usage (NVIDIA): Unavailable ({error})"
    if not gpus:
        return (
            "GPU Usage (NVIDIA): N/A "
            "(GPUtil does not report integrated or non-NVIDIA GPUs)"
        )

    gpu_lines = []
    for index, gpu in enumerate(gpus, start=1):
        load = getattr(gpu, "load", None)
        usage = f"{load * 100:.1f}%" if isinstance(load, (int, float)) else "N/A"
        memory_used = getattr(gpu, "memoryUsed", "N/A")
        memory_total = getattr(gpu, "memoryTotal", "N/A")
        prefix = "GPU Usage (NVIDIA)" if len(gpus) == 1 else f"GPU {index} Usage (NVIDIA)"
        gpu_lines.append(f"{prefix}: {usage} ({memory_used}/{memory_total} MiB)")
    return "\n".join(gpu_lines)


def _format_gpu_specs():
    gpus, error = _probe_nvidia_gpus()
    if error:
        return f"Unavailable: NVIDIA telemetry failed ({error})"
    if not gpus:
        return "No NVIDIA GPU detected (GPUtil does not report non-NVIDIA GPUs)."

    return "\n".join(
        f"{index}. {getattr(gpu, 'name', 'Unknown')} "
        f"({getattr(gpu, 'memoryTotal', 'N/A')} MiB), "
        f"Driver: {getattr(gpu, 'driver', 'Unknown')}"
        for index, gpu in enumerate(gpus, start=1)
    )


def _collect_system_information():
    system_info = platform.uname()
    cpu = _collect_cpu_details()
    return (
        f"System: {system_info.system}\n"
        f"Node Name: {system_info.node}\n"
        f"Release: {system_info.release}\n"
        f"Version: {system_info.version}\n"
        f"Machine: {system_info.machine}\n"
        f"Processor: {cpu['name']}"
    )


def _collect_system_usage(cpu_interval=1):
    cpu_usage = round(psutil.cpu_percent(interval=cpu_interval), 1)
    cpu_count = psutil.cpu_count(logical=True) or "Unknown"
    memory = psutil.virtual_memory()
    ram_usage = round(memory.percent, 1)
    ram_used = round(memory.used / GIB, 1)
    ram_total = round(memory.total / GIB, 1)

    return (
        f"CPU Usage: {cpu_usage}% ({cpu_count} logical CPUs)\n"
        f"RAM Usage: {ram_usage}% ({ram_used}/{ram_total} GiB)\n"
        f"{_format_gpu_usage()}"
    )


def _collect_machine_specs():
    cpu = _collect_cpu_details()

    try:
        ram_total = f"{psutil.virtual_memory().total / GIB:.1f} GiB"
    except (AttributeError, NotImplementedError, OSError):
        ram_total = "Unavailable"

    # Reading DIMM manufacturer/type data through dmidecode requires root on Linux.
    # The bot deliberately avoids sudo prompts and still reports total usable RAM.
    ram_dimms = "Unavailable (privileged hardware probing is disabled)"

    return (
        f"CPU: {cpu['name']}\n"
        f"CPU Architecture: {cpu['architecture']}\n"
        f"CPU Logical CPUs: {cpu['logical_cpus']}\n"
        f"CPU Speed: {cpu['speed_mhz']} MHz\n"
        f"CPU Vendor: {cpu['vendor']}\n"
        f"RAM Total: {ram_total}\n"
        f"RAM DIMMs: {ram_dimms}\n"
        f"GPU Info (NVIDIA via GPUtil):\n{_format_gpu_specs()}"
    )


def _fetch_public_ip():
    response = requests.get(
        "https://api.ipify.org?format=json",
        timeout=PUBLIC_IP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    public_ip = response.json().get("ip")
    if not public_ip:
        raise ValueError("The public IP service returned no IP address")
    return public_ip


def _find_local_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]


def _collect_disk_usage():
    total, used, free = shutil.disk_usage("/")
    return (
        f"Total: {total // GIB} GiB\n"
        f"Used: {used // GIB} GiB\n"
        f"Free: {free // GIB} GiB"
    )


def _retry_after_seconds(exc):
    retry_after = exc.retry_after
    if hasattr(retry_after, "total_seconds"):
        retry_after = retry_after.total_seconds()
    return max(float(retry_after), 0.0)


async def _edit_message_with_retry(message, text, attempts=2):
    """Edit a status message without turning Telegram throttling into a task crash."""
    for attempt in range(attempts):
        try:
            await message.edit_text(text)
            return True
        except RetryAfter as exc:
            if attempt + 1 == attempts:
                LOGGER.warning(
                    "Telegram kept throttling a status-message edit after %d attempts",
                    attempts,
                )
                return False
            await asyncio.sleep(_retry_after_seconds(exc))
        except NetworkError:
            if attempt + 1 == attempts:
                LOGGER.warning(
                    "Telegram status-message edit failed after %d network attempts",
                    attempts,
                    exc_info=True,
                )
                return False
            await asyncio.sleep(NETWORK_EDIT_RETRY_SECONDS)
        except Exception:
            LOGGER.exception("Could not edit a Telegram status message")
            return False
    return False


async def set_bot_menu(application):
    commands = [
        BotCommand("run", "Run a command"),
        BotCommand("stop", "Stop all running work in this chat"),
        BotCommand("splitpdf", "Split PDFs or a replied PDF album"),
        BotCommand("authorize", "Authorize a user (admins)"),
        BotCommand("unauthorize", "Revoke user access (admins)"),
        BotCommand("get_local_ip", "Get your local IP address"),
        BotCommand("get_public_ip", "Get your public IP address"),
        BotCommand("get_system_info", "Get system information"),
        BotCommand("get_machine_specs", "Get machine specifications"),
        BotCommand("get_system_usage", "Get system usage"),
        BotCommand("get_disk_usage", "Get disk usage"),
        BotCommand("monitor_system_usage", "Monitor system usage for 5 minutes"),
    ]
    await application.bot.set_my_commands(commands)


async def _run_status_command(
    update,
    initial_text,
    collector,
    *,
    label,
    error_label,
    render=lambda value: value,
):
    if not is_user_authorized(update.message.from_user):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return

    task = register_current_task(update, label)
    status = None
    animation = None
    try:
        status = await update.message.reply_text(
            animated_status_text(initial_text)
        )
        animation = asyncio.create_task(
            animate_status(
                initial_text,
                lambda text: _edit_message_with_retry(status, text),
            ),
            name=f"status-animation:{label.casefold().replace(' ', '-')}",
        )
        response = await asyncio.to_thread(collector)
        await stop_animation(animation)
        animation = None
        await _edit_message_with_retry(status, render(response))
    except asyncio.CancelledError:
        await asyncio.shield(stop_animation(animation))
        animation = None
        if status is not None:
            await asyncio.shield(
                _edit_message_with_retry(status, f"🛑 {label} stopped.")
            )
        raise
    except Exception as exc:
        await stop_animation(animation)
        animation = None
        if status is not None:
            await _edit_message_with_retry(
                status,
                f"{error_label}: {exc}",
            )
    finally:
        await stop_animation(animation)
        unregister_task(update, task)


async def get_local_ip(update: Update, context) -> None:
    await _run_status_command(
        update,
        "Looking up the local IP…",
        _find_local_ip,
        label="Local IP lookup",
        error_label="Error retrieving local IP",
        render=lambda value: f"Your local IP is: {value}",
    )


async def get_system_info(update: Update, context) -> None:
    await _run_status_command(
        update,
        "Collecting system information…",
        _collect_system_information,
        label="System information",
        error_label="Error retrieving system info",
    )


async def get_disk_usage(update: Update, context) -> None:
    await _run_status_command(
        update,
        "Checking disk usage…",
        _collect_disk_usage,
        label="Disk usage check",
        error_label="Error retrieving disk usage",
    )


async def get_public_ip(update: Update, context) -> None:
    await _run_status_command(
        update,
        "Looking up the public IP…",
        _fetch_public_ip,
        label="Public IP lookup",
        error_label="Error retrieving public IP",
        render=lambda value: f"Your public IP is: {value}",
    )


async def get_system_usage(update: Update, context) -> None:
    await _run_status_command(
        update,
        "Measuring system usage…",
        _collect_system_usage,
        label="System usage check",
        error_label="Error retrieving system usage",
    )


async def get_machine_specs(update: Update, context) -> None:
    await _run_status_command(
        update,
        "Collecting machine specifications…",
        _collect_machine_specs,
        label="Machine specifications",
        error_label="Error retrieving machine specs",
    )


async def _run_usage_monitor(message):
    loop = asyncio.get_running_loop()
    end_time = loop.time() + MONITOR_DURATION_SECONDS
    next_tick = loop.time()
    frame = 0

    try:
        while loop.time() < end_time:
            usage = await asyncio.to_thread(_collect_system_usage, 0.2)
            time_left = max(0, int(end_time - loop.time()))
            minutes, seconds = divmod(time_left, 60)
            response = (
                f"{STATUS_FRAMES[frame % len(STATUS_FRAMES)]} "
                f"Monitoring system usage — {minutes:02d}:{seconds:02d} remaining\n"
                f"{usage}"
            )
            await _edit_message_with_retry(message, response)
            frame += 1

            next_tick += MONITOR_INTERVAL_SECONDS
            now = loop.time()
            if next_tick <= now:
                # A slow Telegram edit or RetryAfter missed this tick. Resume
                # one interval from now instead of emitting a catch-up burst.
                next_tick = now + MONITOR_INTERVAL_SECONDS
            delay = max(next_tick - now, 0)
            await asyncio.sleep(min(delay, max(end_time - loop.time(), 0)))

        await _edit_message_with_retry(message, "🛑 Stopped monitoring system usage.")
    except asyncio.CancelledError:
        await asyncio.shield(
            _edit_message_with_retry(message, "🛑 System monitoring stopped.")
        )
        raise
    except Exception as exc:
        LOGGER.exception("System-usage monitoring failed")
        await _edit_message_with_retry(
            message,
            f"⚠️ Error retrieving system usage: {exc}",
        )


def _monitor_key(update):
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None) or update.message.from_user
    chat_id = getattr(chat, "id", None)
    user_id = getattr(user, "id", None)
    # Messages constructed in tests or unusual updates may not expose a chat. The
    # user id still prevents duplicate work for that command sender.
    return (chat_id if chat_id is not None else user_id, user_id)


def _forget_monitor(key, completed_task):
    if _ACTIVE_MONITORS.get(key) is completed_task:
        _ACTIVE_MONITORS.pop(key, None)


async def monitor_system_usage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_user_authorized(update.message.from_user):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return

    key = _monitor_key(update)
    existing_task = _ACTIVE_MONITORS.get(key)
    if existing_task is not None and not existing_task.done():
        await update.message.reply_text(
            "🟢 System monitoring is already running in this chat for you."
        )
        return

    startup_task = asyncio.create_task(
        update.message.reply_text(
            animated_status_text(
                "Starting system monitor — gathering the first reading…"
            )
        )
    )
    try:
        message = await asyncio.shield(startup_task)
    except asyncio.CancelledError:
        # Let the in-flight Telegram request resolve so a delivered startup
        # message never remains permanently active-looking after /stop.
        try:
            message = await asyncio.shield(startup_task)
        except Exception:
            message = None
        if message is not None:
            await asyncio.shield(
                _edit_message_with_retry(
                    message,
                    "🛑 System monitoring stopped.",
                )
            )
        raise
    task = context.application.create_task(_run_usage_monitor(message))
    _ACTIVE_MONITORS[key] = task
    register_task(update, task, "System monitor")
    task.add_done_callback(lambda completed: _forget_monitor(key, completed))
