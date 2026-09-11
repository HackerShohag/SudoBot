"""Secure file delivery for the /get Telegram command."""

import asyncio
import logging
from pathlib import Path
import shlex
import stat

from telegram import InputFile, Update
from telegram.error import NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from bot.status_animation import (
    animate_status,
    animated_status_text,
    stop_animation,
)
from bot.utils import is_admin, is_super_admin


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 49 * 1024 * 1024
PRIVATE_KEY_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
PROTECTED_DIRECTORY_NAMES = {".ssh", ".gnupg"}
PROTECTED_FILE_NAMES = {
    "authorized_keys",
    "credentials.json",
    "service-account.json",
}
PROTECTED_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
SAFE_ENV_TEMPLATES = {".env.example", ".env.sample"}


class FileAccessError(ValueError):
    """A requested file is missing, unsafe, or outside the caller's scope."""


def _retry_after_seconds(value) -> float:
    if hasattr(value, "total_seconds"):
        value = value.total_seconds()
    return max(float(value), 0.1)


async def _edit_file_status(message, text) -> bool:
    """Best-effort status edit with bounded flood/network retries."""
    for attempt in range(2):
        try:
            await message.edit_text(text)
            return True
        except RetryAfter as exc:
            if attempt:
                break
            await asyncio.sleep(_retry_after_seconds(exc.retry_after))
        except NetworkError:
            if attempt:
                break
            await asyncio.sleep(1)
        except TelegramError:
            break
    logger.warning("Could not update /get status message")
    return False


def _is_environment_secret(path: Path) -> bool:
    name = path.name.casefold()
    return name not in SAFE_ENV_TEMPLATES and (
        name == ".env" or name.startswith(".env.")
    )


def _is_protected_file(path: Path) -> bool:
    lowered_parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return (
        _is_environment_secret(path)
        or bool(lowered_parts & PROTECTED_DIRECTORY_NAMES)
        or name in PRIVATE_KEY_NAMES
        or name in PROTECTED_FILE_NAMES
        or path.suffix.casefold() in PROTECTED_SUFFIXES
    )


def _inside_project(path: Path) -> bool:
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    return True


def resolve_file_for_user(requested: str, user) -> Path:
    """Resolve a requested file under the caller's role-based policy."""
    if not is_admin(user):
        raise FileAccessError("Only admins can retrieve files.")

    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileAccessError("The requested file was not found.") from exc

    super_admin = is_super_admin(user)
    if not super_admin and not _inside_project(resolved):
        raise FileAccessError(
            "Admins can retrieve files only from the bot project."
        )
    if not super_admin and _is_protected_file(resolved):
        raise FileAccessError(
            "This file contains protected credentials or environment data."
        )

    try:
        file_stat = resolved.stat()
    except OSError as exc:
        raise FileAccessError("The requested file cannot be inspected.") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise FileAccessError("The requested path must be a regular file.")
    if file_stat.st_size > MAX_FILE_BYTES:
        raise FileAccessError(
            "The requested file exceeds the 49 MB Telegram upload limit."
        )
    return resolved


async def get_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload one permitted relative or absolute path to the current chat."""
    user = update.message.from_user
    if not is_admin(user):
        await update.message.reply_text("❌ Only admins can retrieve files.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /get <filename, relative path, or absolute path>"
        )
        return

    requested = " ".join(context.args).strip()
    try:
        quoted_path = shlex.split(requested)
    except ValueError:
        quoted_path = []
    if len(quoted_path) == 1:
        requested = quoted_path[0]
    try:
        path = resolve_file_for_user(requested, user)
    except FileAccessError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    status = await update.message.reply_text(
        animated_status_text(f"Uploading {path.name}…")
    )
    animation = asyncio.create_task(
        animate_status(
            f"Uploading {path.name}…",
            lambda text: _edit_file_status(status, text),
        ),
        name="file-upload-animation",
    )
    try:
        with path.open("rb") as source:
            await update.message.reply_document(
                document=InputFile(source, filename=path.name),
                caption=path.name,
                read_timeout=120,
                write_timeout=300,
                connect_timeout=30,
                pool_timeout=30,
            )
    except (OSError, TelegramError) as exc:
        await stop_animation(animation)
        animation = None
        logger.warning("Could not deliver requested file %s: %s", path, exc)
        await _edit_file_status(
            status,
            f"❌ Could not upload {path.name}: {exc}",
        )
    except asyncio.CancelledError:
        await asyncio.shield(stop_animation(animation))
        animation = None
        await asyncio.shield(
            _edit_file_status(status, "🛑 File upload stopped.")
        )
        raise
    else:
        await stop_animation(animation)
        animation = None
        await _edit_file_status(status, f"✅ Uploaded {path.name}.")
    finally:
        await stop_animation(animation)
