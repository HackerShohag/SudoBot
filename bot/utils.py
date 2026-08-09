import sqlite3
import asyncio
import logging
import multiprocessing
import queue
import shlex
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

from telegram import InputFile, Update
from telegram.ext import ContextTypes, CallbackContext
from telegram.error import NetworkError, RetryAfter, TelegramError
from pathlib import Path

import fitz
from PIL import Image, ImageChops

from bot.config import (
    BOT_TOKEN,
    SUPER_ADMIN_USERNAME,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
)
from bot.mtproto import (
    MtprotoConfigurationError,
    MtprotoDependencyError,
    MtprotoDownloader,
    MtprotoError,
)

UPLOAD_DIR = "uploads"
DB_PATH = "db/authorized_users.db"
PDF_UPLOAD_ATTEMPTS = 3
PDF_UPLOAD_READ_TIMEOUT = 120
PDF_UPLOAD_WRITE_TIMEOUT = 300
PDF_UPLOAD_CONNECT_TIMEOUT = 30
PDF_UPLOAD_POOL_TIMEOUT = 30
PDF_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
PDF_STATUS_EDIT_INTERVAL = 2.0
PDF_UPLOAD_STATUS_INTERVAL = 1.0
PDF_MEDIA_GROUP_CACHE_LIMIT = 100
PDF_MEDIA_GROUP_CACHE_TTL_SECONDS = 24 * 60 * 60
PDF_MEDIA_GROUP_CACHE_KEY = "_pdf_media_groups"

logger = logging.getLogger(__name__)
_mtproto_downloader = None


@dataclass(frozen=True)
class SplitResult:
    bw_path: Optional[Path]
    color_path: Optional[Path]
    bw_pages: int
    color_pages: int
    guide: str


@dataclass(frozen=True)
class PdfSource:
    document: object
    chat_id: int
    message_id: int
    media_group_id: Optional[str] = None


@dataclass(frozen=True)
class PdfAlbumSelection:
    sources: tuple[PdfSource, ...]
    verified_complete: bool


class PdfDownloadError(Exception):
    """A selected PDF could not be downloaded from Telegram."""


class _UploadProgressFile:
    """Delegate a binary file while counting bytes consumed by HTTPX."""

    def __init__(self, raw_file, total_bytes):
        self._raw_file = raw_file
        self.total_bytes = max(int(total_bytes), 0)
        self.transferred = 0
        self.name = raw_file.name

    def read(self, size=-1):
        chunk = self._raw_file.read(size)
        self.transferred = min(
            self.total_bytes,
            self.transferred + len(chunk),
        )
        return chunk

    def seek(self, offset, whence=0):
        position = self._raw_file.seek(offset, whence)
        # HTTPX seeks to zero immediately before rendering the multipart body.
        # Resetting here also makes every network retry begin at a truthful 0%.
        if position == 0:
            self.transferred = 0
        return position

    def tell(self):
        return self._raw_file.tell()

    def fileno(self):
        return self._raw_file.fileno()

    def __getattr__(self, name):
        return getattr(self._raw_file, name)


class _PdfStatus:
    """Best-effort editor for the one status message used by a PDF job."""

    def __init__(
        self,
        *,
        message=None,
        bot=None,
        chat_id=None,
        message_id=None,
        initial_text=None,
        prefix=None,
    ):
        self._message = message
        self._bot = bot
        self.chat_id = chat_id
        self.message_id = message_id or getattr(message, "message_id", None)
        self._prefix = prefix
        self._last_text = initial_text
        self._last_edit = time.monotonic() if initial_text else 0.0
        self._lock = asyncio.Lock()

    async def update(self, text, *, force=False):
        """Edit the status without allowing edit failures to stop the job."""
        text = str(text)
        if self._prefix:
            text = f"{self._prefix}\n{text}"
        text = text[:4096]
        async with self._lock:
            if text == self._last_text:
                return True
            if (
                not force
                and time.monotonic() - self._last_edit
                < PDF_STATUS_EDIT_INTERVAL
            ):
                return False

            for attempt in range(2 if force else 1):
                try:
                    if callable(getattr(self._message, "edit_text", None)):
                        await self._message.edit_text(text)
                    elif (
                        self._bot is not None
                        and callable(
                            getattr(self._bot, "edit_message_text", None)
                        )
                        and self.chat_id is not None
                        and self.message_id is not None
                    ):
                        await self._bot.edit_message_text(
                            chat_id=self.chat_id,
                            message_id=self.message_id,
                            text=text,
                        )
                    else:
                        return False
                    self._last_text = text
                    self._last_edit = time.monotonic()
                    return True
                except RetryAfter as exc:
                    if attempt or not force:
                        return False
                    retry_after = exc.retry_after
                    delay = (
                        retry_after.total_seconds()
                        if hasattr(retry_after, "total_seconds")
                        else float(retry_after)
                    )
                    await asyncio.sleep(delay + 0.1)
                except NetworkError:
                    if attempt or not force:
                        return False
                    await asyncio.sleep(1)
                except TelegramError:
                    return False
                except Exception:
                    return False
        return False

    def set_prefix(self, prefix):
        self._prefix = prefix


async def _new_pdf_status(
    message,
    context,
    text,
    *,
    prefix=None,
    reply_to_message_id=None,
):
    rendered_text = f"{prefix}\n{text}" if prefix else text
    reply_kwargs = {}
    if reply_to_message_id is not None:
        reply_kwargs = {
            "reply_to_message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }
    sent = await message.reply_text(rendered_text[:4096], **reply_kwargs)
    return _PdfStatus(
        message=sent,
        bot=getattr(context, "bot", None),
        chat_id=getattr(getattr(message, "chat", None), "id", None),
        message_id=getattr(sent, "message_id", None),
        initial_text=rendered_text[:4096],
        prefix=prefix,
    )


def _pending_pdf_status(context, pending_split):
    return _PdfStatus(
        bot=getattr(context, "bot", None),
        chat_id=pending_split.get("chat_id"),
        message_id=pending_split.get("prompt_message_id"),
    )


def _human_file_size(size):
    if not isinstance(size, (int, float)) or size < 0:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return None


def _download_status_text(document):
    size = _human_file_size(getattr(document, "file_size", None))
    size_text = f" ({size})" if size else ""
    return f"⬇️ Downloading selected PDF{size_text}…"


def _requires_mtproto_download(document):
    if getattr(document, "mtproto_only", False):
        return True
    file_size = getattr(document, "file_size", None)
    return (
        isinstance(file_size, (int, float))
        and file_size > PDF_DOWNLOAD_LIMIT_BYTES
    )


def _get_mtproto_downloader():
    """Return the shared direct Telegram client for downloads and albums."""
    global _mtproto_downloader
    if _mtproto_downloader is None:
        _mtproto_downloader = MtprotoDownloader(
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
            bot_token=BOT_TOKEN,
        )
    return _mtproto_downloader


async def close_mtproto_downloader():
    """Best-effort shutdown for the optional in-process MTProto client."""
    global _mtproto_downloader
    downloader = _mtproto_downloader
    _mtproto_downloader = None
    if downloader is None:
        return
    try:
        await downloader.close()
    except MtprotoError:
        logger.warning(
            "Could not close the MTProto large-file client cleanly",
            exc_info=True,
        )


def _direct_download_setup_error(document, detail):
    size = _human_file_size(getattr(document, "file_size", None))
    size_text = f" ({size})" if size else ""
    return PdfDownloadError(
        f"❌ Automatic direct Telegram download{size_text} is unavailable: "
        f"{detail}\n\n"
        "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env, install "
        "requirements.txt, and restart the bot."
    )


def _direct_download_progress_text(document, current, reported_total):
    configured_total = getattr(document, "file_size", None)
    total = (
        configured_total
        if isinstance(configured_total, (int, float)) and configured_total > 0
        else reported_total
    )
    if isinstance(total, (int, float)) and total > 0:
        current = min(max(current, 0), total)
        percent = round((current / total) * 100)
        return (
            "⬇️ Downloading PDF directly: "
            f"{_human_file_size(current)} / {_human_file_size(total)} "
            f"({percent}%)…"
        )
    return f"⬇️ Downloading PDF directly: {_human_file_size(current)}…"


def _split_completion_text(result, duplex):
    summary = (
        "✅ PDF split complete.\n"
        f"Mode: {'Duplex' if duplex else 'Simplex'}\n"
        f"B&W pages: {result.bw_pages}\n"
        f"Color pages: {result.color_pages}"
    )
    if not duplex or not result.guide:
        return summary
    detail_prefix = "\n\nPrinting guide:\n"
    available = 4096 - len(summary) - len(detail_prefix)
    guide = result.guide[:available]
    return f"{summary}{detail_prefix}{guide}"


def is_color_page(page):
    """Return True when the rendered PDF page contains non-gray pixels."""
    pix = page.get_pixmap(
        matrix=fitz.Matrix(1, 1),
        colorspace=fitz.csRGB,
        alpha=False,
    )
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    red, green, blue = image.split()
    red_green_diff = ImageChops.difference(red, green).getextrema()
    green_blue_diff = ImageChops.difference(green, blue).getextrema()
    return red_green_diff != (0, 0) or green_blue_diff != (0, 0)


def split_for_manual_color(
    input_path,
    duplex=False,
    output_dir=None,
    progress_callback=None,
):
    """Split a PDF into B&W/color outputs for manual hybrid printing."""
    input_path = Path(input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"File '{input_path}' not found.")

    output_dir = Path(output_dir) if output_dir else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = input_path.stem
    guide_lines = [
        f"Manual color printing guide for: {input_path.name}",
        f"Mode: {'Double-Sided (Duplex)' if duplex else 'Single-Sided (Simplex)'}",
    ]

    document = fitz.open(input_path)
    bw_document = fitz.open()
    color_document = fitz.open()

    if document.needs_pass:
        document.close()
        bw_document.close()
        color_document.close()
        raise ValueError("Password-protected PDFs are not supported.")
    if len(document) == 0:
        document.close()
        bw_document.close()
        color_document.close()
        raise ValueError("The PDF contains no pages.")

    try:
        original_page_count = len(document)
        if progress_callback:
            progress_callback(0, original_page_count)
        for index in range(original_page_count):
            page = document[index]
            page_rect = page.rect

            if is_color_page(page):
                color_document.insert_pdf(
                    document,
                    from_page=index,
                    to_page=index,
                )
                if duplex:
                    bw_document.new_page(
                        width=page_rect.width,
                        height=page_rect.height,
                    )
                    sheet_number = (index // 2) + 1
                    side = "Front" if index % 2 == 0 else "Back"
                    guide_lines.append(
                        f"{base_name}_Color.pdf page {len(color_document)} -> "
                        f"overprint on B&W sheet {sheet_number} ({side})"
                    )
                else:
                    guide_lines.append(
                        f"{base_name}_Color.pdf page {len(color_document)} -> "
                        f"insert as document page {index + 1}"
                    )
            else:
                bw_document.insert_pdf(
                    document,
                    from_page=index,
                    to_page=index,
                )
            if progress_callback:
                progress_callback(index + 1, original_page_count)

        if duplex and original_page_count % 2:
            last_page = document[original_page_count - 1]
            bw_document.new_page(
                width=last_page.rect.width,
                height=last_page.rect.height,
            )

        bw_path = output_dir / f"{base_name}_BW.pdf"
        color_path = output_dir / f"{base_name}_Color.pdf"
        bw_pages = len(bw_document)
        color_pages = len(color_document)

        if bw_pages:
            bw_document.save(bw_path)
        else:
            bw_path = None

        if color_pages:
            color_document.save(color_path)
        else:
            color_path = None
    finally:
        color_document.close()
        bw_document.close()
        document.close()

    guide_lines.append(
        f"Done: {bw_pages} B&W page(s), {color_pages} color page(s)."
    )
    return SplitResult(
        bw_path=bw_path,
        color_path=color_path,
        bw_pages=bw_pages,
        color_pages=color_pages,
        guide="\n".join(guide_lines),
    )


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS authorized_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            user_id INTEGER UNIQUE,
            full_name TEXT,
            added_by TEXT,
            role TEXT DEFAULT 'user'  -- New column for user roles
        )
    """)
    conn.commit()
    conn.close()

init_db()

def _telegram_identity(user_or_id):
    if isinstance(user_or_id, int):
        return user_or_id, None
    return user_or_id.id, getattr(user_or_id, "username", None)


def get_user_role(user_or_id) -> Optional[str]:
    """Get a role and bind a pending username authorization when possible."""
    user_id, username = _telegram_identity(user_or_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM authorized_users WHERE user_id = ?", (user_id,))
    role = cursor.fetchone()
    if role is None and username:
        try:
            cursor.execute(
                """
                UPDATE authorized_users
                SET user_id = ?
                WHERE user_id IS NULL AND username = ? COLLATE NOCASE
                """,
                (user_id, username),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
        cursor.execute(
            "SELECT role FROM authorized_users WHERE user_id = ?",
            (user_id,),
        )
        role = cursor.fetchone()
    conn.close()
    return role[0] if role else None

def is_user_authorized(user_or_id) -> bool:
    return _is_configured_super_admin(user_or_id) or (
        get_user_role(user_or_id) is not None
    )

def is_admin(user_or_id) -> bool:
    return _is_configured_super_admin(user_or_id) or (
        get_user_role(user_or_id) == "admin"
    )


def _is_configured_super_admin(user) -> bool:
    """Match the explicitly configured owner without requiring DB seeding."""
    if isinstance(user, int):
        return False
    username = (getattr(user, "username", None) or "").lstrip("@").casefold()
    return (
        bool(SUPER_ADMIN_USERNAME)
        and username == SUPER_ADMIN_USERNAME
    )


def is_super_admin(user) -> bool:
    """Allow host-level operations only for the configured primary admin."""
    return _is_configured_super_admin(user)


async def authorize_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.message.from_user

    if not is_admin(admin):
        await update.message.reply_text("❌ You are not authorized to add users.")
        return

    if len(context.args) == 0 and not update.message.reply_to_message:
        await update.message.reply_text(
            "Usage: /authorize <username> [admin|user], or reply with "
            "/authorize [admin|user]."
        )
        return

    replied_user = (
        update.message.reply_to_message.from_user
        if update.message.reply_to_message
        else None
    )
    if replied_user:
        username = replied_user.username
        user_id_to_add = replied_user.id
        full_name = replied_user.full_name
        if context.args and context.args[0].lower() in {"admin", "user"}:
            role = context.args[0].lower()
        elif len(context.args) > 1:
            role = context.args[1].lower()
        else:
            role = "user"
    else:
        username = context.args[0].lstrip('@')
        role = context.args[1].lower() if len(context.args) > 1 else "user"
        full_name = None
        user_id_to_add = None

    if role not in ["admin", "user"]:
        await update.message.reply_text("❌ Invalid role. Use 'admin' or 'user'.")
        return
    if not username and user_id_to_add is None:
        await update.message.reply_text("❌ That user does not have a Telegram username.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        existing = None
        if user_id_to_add is not None:
            cursor.execute(
                "SELECT id FROM authorized_users WHERE user_id = ?",
                (user_id_to_add,),
            )
            existing = cursor.fetchone()
        if existing is None and username:
            cursor.execute(
                """
                SELECT id, user_id FROM authorized_users
                WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            )
            username_row = cursor.fetchone()
            if (
                username_row
                and user_id_to_add is not None
                and username_row[1] not in (None, user_id_to_add)
            ):
                await update.message.reply_text(
                    "❌ That username is already bound to another Telegram account."
                )
                return
            existing = username_row[:1] if username_row else None

        if existing:
            cursor.execute(
                """
                UPDATE authorized_users
                SET username = COALESCE(?, username),
                    user_id = COALESCE(?, user_id),
                    full_name = COALESCE(?, full_name),
                    added_by = ?,
                    role = ?
                WHERE id = ?
                """,
                (
                    username,
                    user_id_to_add,
                    full_name,
                    admin.username,
                    role,
                    existing[0],
                ),
            )
            action = "updated"
        else:
            cursor.execute(
                """
                INSERT INTO authorized_users
                    (username, user_id, full_name, added_by, role)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    username,
                    user_id_to_add,
                    full_name,
                    admin.username,
                    role,
                ),
            )
            action = "authorized"
        conn.commit()
        display_name = f"@{username}" if username else f"ID {user_id_to_add}"
        if user_id_to_add is None:
            await update.message.reply_text(
                f"✅ User {display_name} {action} as {role}. "
                "Their Telegram ID will be linked when they next use the bot."
            )
        else:
            await update.message.reply_text(
                f"✅ User {display_name} {action} as {role}."
            )
    except sqlite3.IntegrityError:
        conn.rollback()
        await update.message.reply_text(
            "❌ Could not authorize this identity because it conflicts with "
            "another authorized account."
        )
    finally:
        conn.close()

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revoke a user's access; used by /unauthorize and the /remove alias."""
    admin = update.message.from_user

    if not is_admin(admin):
        await update.message.reply_text("❌ You are not authorized to revoke users.")
        return

    if len(context.args) == 0 and not update.message.reply_to_message:
        await update.message.reply_text(
            "Usage: /unauthorize <username>, or reply to a user's message "
            "with /unauthorize."
        )
        return

    replied_user = (
        update.message.reply_to_message.from_user
        if update.message.reply_to_message
        else None
    )
    if len(context.args) > 0:
        username = context.args[0].lstrip('@')
        user_id_to_remove = None
    elif replied_user:
        username = replied_user.username
        user_id_to_remove = replied_user.id
    else:
        username = None
        user_id_to_remove = None

    if not username and user_id_to_remove is None:
        await update.message.reply_text(
            "❌ Specify a username, or reply to the user whose access "
            "should be revoked."
        )
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        if user_id_to_remove is not None:
            cursor.execute(
                """
                SELECT id, username FROM authorized_users
                WHERE user_id = ?
                """,
                (user_id_to_remove,),
            )
        else:
            cursor.execute(
                """
                SELECT id, username FROM authorized_users
                WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            )
        target = cursor.fetchone()

        target_usernames = {
            value.lstrip("@").casefold()
            for value in (username, target[1] if target else None)
            if value
        }
        if (
            SUPER_ADMIN_USERNAME
            and SUPER_ADMIN_USERNAME in target_usernames
        ):
            await update.message.reply_text(
                "❌ The configured super admin cannot be unauthorized."
            )
            return

        if target is None:
            removed = 0
        else:
            cursor.execute(
                "DELETE FROM authorized_users WHERE id = ?",
                (target[0],),
            )
            removed = cursor.rowcount
            conn.commit()
    finally:
        conn.close()

    if removed:
        display_name = f"@{username}" if username else f"ID {user_id_to_remove}"
        await update.message.reply_text(
            f"✅ Access revoked for {display_name}."
        )
    else:
        await update.message.reply_text("❌ Authorized user not found.")


def _media_group_cache(context):
    storage = getattr(context, "bot_data", None)
    if not isinstance(storage, dict):
        storage = context.chat_data
    return storage.setdefault(PDF_MEDIA_GROUP_CACHE_KEY, {})


def _prune_media_group_cache(cache, now):
    expired = [
        key
        for key, value in cache.items()
        if now - value["updated_at"] > PDF_MEDIA_GROUP_CACHE_TTL_SECONDS
    ]
    for key in expired:
        cache.pop(key, None)
    while len(cache) > PDF_MEDIA_GROUP_CACHE_LIMIT:
        oldest = min(
            cache,
            key=lambda key: cache[key]["updated_at"],
        )
        cache.pop(oldest, None)


def _remember_media_group_document(context, message, chat_id):
    """Cache album metadata only; never download an unselected document."""
    media_group_id = getattr(message, "media_group_id", None)
    document = getattr(message, "document", None)
    message_id = getattr(message, "message_id", None)
    if (
        not media_group_id
        or not isinstance(message_id, int)
        or not _is_pdf_document(document)
    ):
        return

    now = time.monotonic()
    cache = _media_group_cache(context)
    _prune_media_group_cache(cache, now)
    key = (chat_id, str(media_group_id))
    entry = cache.setdefault(
        key,
        {"updated_at": now, "items": {}},
    )
    entry["updated_at"] = now
    entry["items"][message_id] = PdfSource(
        document=document,
        chat_id=chat_id,
        message_id=message_id,
        media_group_id=str(media_group_id),
    )
    _prune_media_group_cache(cache, now)


async def observe_pdf_upload(update: Update, context: CallbackContext) -> None:
    """Passively remember PDF album members without downloading or replying."""
    message = getattr(update, "effective_message", None) or update.message
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is not None:
        _remember_media_group_document(context, message, chat_id)


async def handle_file_upload(update: Update, context: CallbackContext) -> None:
    _remember_media_group_document(
        context,
        update.message,
        update.effective_chat.id,
    )
    document = update.message.document
    caption_args = _splitpdf_caption_args(update.message.caption)
    pending_split = context.user_data.get("pending_pdf_split")
    replied_message = getattr(update.message, "reply_to_message", None)
    is_pending_reply = bool(
        pending_split
        and pending_split.get("chat_id") == update.effective_chat.id
        and replied_message
        and replied_message.message_id
        == pending_split.get("prompt_message_id")
    )

    # With group privacy disabled the bot receives every document. Ignore all
    # files unless the sender explicitly selected one for PDF splitting.
    if caption_args is None and not is_pending_reply:
        return

    if not is_user_authorized(update.message.from_user):
        await update.message.reply_text("❌ You are not authorized to split files.")
        return

    if not _is_pdf_document(document):
        await update.message.reply_text("❌ The selected file is not a PDF.")
        return

    if is_pending_reply:
        status = _pending_pdf_status(context, pending_split)
        await status.update(_download_status_text(document), force=True)
    else:
        status = await _new_pdf_status(
            update.message,
            context,
            _download_status_text(document),
        )

    try:
        file_path = await _download_pdf_document(
            context,
            document,
            update.effective_chat.id,
            update.message.message_id,
            status=status,
        )
    except PdfDownloadError as exc:
        await status.update(str(exc), force=True)
        return

    _remember_chat_pdf(
        context,
        update.message.message_id,
        file_path,
    )

    if caption_args is not None:
        context.user_data.pop("pending_pdf_split", None)
        await split_pdf(
            update,
            context,
            args=caption_args,
            input_path=file_path,
            status=status,
        )
        return

    if is_pending_reply:
        context.user_data.pop("pending_pdf_split", None)
        await split_pdf(
            update,
            context,
            args=pending_split["args"],
            input_path=file_path,
            status=status,
        )
        return


def _splitpdf_caption_args(caption):
    if not caption:
        return None
    try:
        parts = shlex.split(caption)
    except ValueError:
        return []
    if not parts or parts[0].split("@", 1)[0].lower() not in {
        "/splitpdf",
        "/printer",
    }:
        return None
    return parts[1:]


def _duplex_from_args(args):
    if not args:
        return False
    normalized = [arg.lower() for arg in args]
    if normalized in (["-d"], ["--duplex"], ["duplex"]):
        return True
    if normalized in (["--simplex"], ["simplex"]):
        return False
    raise ValueError(
        "Usage: /splitpdf [--duplex]\n"
        "Upload a PDF first, reply to a PDF or PDF album, or use the command "
        "as its caption."
    )


def _is_pdf_document(document):
    if document is None:
        return False
    file_name = (getattr(document, "file_name", None) or "").lower()
    mime_type = (getattr(document, "mime_type", None) or "").lower()
    return file_name.endswith(".pdf") or mime_type == "application/pdf"


def _upload_file_path(chat_id, message_id, file_name):
    safe_name = Path(file_name or "upload").name
    return (
        Path(UPLOAD_DIR)
        / str(chat_id)
        / str(message_id)
        / safe_name
    )


def _remember_chat_pdf(context, message_id, file_path):
    file_path = str(file_path)
    context.chat_data.setdefault("uploaded_pdfs", {})[
        str(message_id)
    ] = file_path


def _pdf_from_attachment(attachment):
    attachments = (
        attachment
        if isinstance(attachment, (list, tuple))
        else [attachment]
    )
    return next(
        (
            item
            for item in attachments
            if _is_pdf_document(item)
        ),
        None,
    )


def _reply_candidates(message):
    if message is None:
        return []
    replied = getattr(message, "reply_to_message", None)
    candidates = [replied]
    if replied is not None:
        candidates.append(getattr(replied, "external_reply", None))
        candidates.append(getattr(replied, "reply_to_message", None))
    candidates.append(getattr(message, "external_reply", None))
    return [candidate for candidate in candidates if candidate is not None]


def _find_replied_pdf_document(message):
    """Find a PDF in normal, quoted, or cross-chat Telegram replies."""
    for candidate in _reply_candidates(message):
        document = getattr(candidate, "document", None)
        if _is_pdf_document(document):
            return document
        document = _pdf_from_attachment(
            getattr(candidate, "effective_attachment", None)
        )
        if document:
            return document
    return None


def _reply_message_reference(message, current_chat_id):
    """Return the source (chat_id, message_id) for a Telegram reply."""
    for candidate in _reply_candidates(message):
        message_id = getattr(candidate, "message_id", None)
        if message_id is None:
            continue
        chat = getattr(candidate, "chat", None)
        chat_id = getattr(chat, "id", None) or current_chat_id
        return chat_id, message_id
    return None


def _replied_media_group_reference(message, current_chat_id):
    """Return (chat, message, group) when a reply targets an album member."""
    for candidate in _reply_candidates(message):
        media_group_id = getattr(candidate, "media_group_id", None)
        message_id = getattr(candidate, "message_id", None)
        if not media_group_id or not isinstance(message_id, int):
            continue
        chat = getattr(candidate, "chat", None)
        chat_id = getattr(chat, "id", None) or current_chat_id
        return chat_id, message_id, str(media_group_id)
    return None


def _cached_media_group_sources(context, chat_id, media_group_id):
    now = time.monotonic()
    cache = _media_group_cache(context)
    _prune_media_group_cache(cache, now)
    entry = cache.get((chat_id, str(media_group_id)))
    if entry is None:
        return []
    return [
        entry["items"][message_id]
        for message_id in sorted(entry["items"])
    ]


async def _resolve_replied_media_group_sources(update, context):
    """Resolve the PDFs in the exact album selected by the reply."""
    message = getattr(update, "effective_message", None) or update.message
    reference = _replied_media_group_reference(
        message,
        update.effective_chat.id,
    )
    if reference is None:
        return None
    source_chat_id, source_message_id, media_group_id = reference
    cached = _cached_media_group_sources(
        context,
        source_chat_id,
        media_group_id,
    )
    sources = {source.message_id: source for source in cached}
    lookup_succeeded = False

    try:
        downloader = _get_mtproto_downloader()
        recovered = await downloader.get_media_group_documents(
            source_chat_id,
            source_message_id,
        )
        lookup_succeeded = True
    except (MtprotoConfigurationError, MtprotoDependencyError) as exc:
        if len(sources) <= 1:
            raise PdfDownloadError(
                "❌ I detected a Telegram album but could not retrieve all "
                "of its files. Disable Privacy Mode for this bot in "
                "BotFather, or set working TELEGRAM_API_ID and "
                "TELEGRAM_API_HASH values, restart, and try again."
            ) from exc
    except MtprotoError as exc:
        logger.warning(
            "MTProto album lookup failed for chat %s message %s",
            source_chat_id,
            source_message_id,
            exc_info=True,
        )
        if len(sources) <= 1:
            raise PdfDownloadError(
                "❌ Telegram identified an album, but the bot could not "
                "access all of its files. Make sure the bot is still a "
                "member of this group and try again."
            ) from exc
    else:
        for document in recovered:
            document_message_id = getattr(document, "message_id", None)
            document_media_group_id = getattr(
                document,
                "media_group_id",
                None,
            )
            if (
                not isinstance(document_message_id, int)
                or str(document_media_group_id) != media_group_id
                or not _is_pdf_document(document)
            ):
                continue
            sources.setdefault(
                document_message_id,
                PdfSource(
                    document=document,
                    chat_id=source_chat_id,
                    message_id=document_message_id,
                    media_group_id=media_group_id,
                ),
            )

    if not sources:
        raise PdfDownloadError(
            "❌ The selected Telegram album does not contain any PDF files."
        )
    return PdfAlbumSelection(
        sources=tuple(sources[message_id] for message_id in sorted(sources)),
        verified_complete=lookup_succeeded,
    )


def _has_explicit_reply(message):
    return bool(
        getattr(message, "reply_to_message", None)
        or getattr(message, "external_reply", None)
    )


def _cached_replied_pdf(message, current_chat_id, context):
    reference = _reply_message_reference(message, current_chat_id)
    if reference is None:
        return None
    source_chat_id, message_id = reference
    message_dir = (
        Path(UPLOAD_DIR)
        / str(source_chat_id)
        / str(message_id)
    )

    if source_chat_id == current_chat_id:
        cached = context.chat_data.get("uploaded_pdfs", {}).get(
            str(message_id)
        )
        if (
            cached
            and Path(cached).is_file()
            and Path(cached).parent == message_dir
        ):
            return cached

    if not message_dir.is_dir():
        return None
    return next(
        (
            str(path)
            for path in message_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        None,
    )


async def _download_pdf_document(
    context,
    document,
    source_chat_id,
    source_message_id,
    *,
    status=None,
):
    file_path = _upload_file_path(
        source_chat_id,
        source_message_id,
        document.file_name or "upload.pdf",
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve an already-complete cached copy if Telegram fails while
    # redownloading the same message.
    download_path = (
        file_path.with_suffix(f"{file_path.suffix}.part")
        if file_path.exists()
        else file_path
    )

    async def download_with_mtproto():
        try:
            downloader = _get_mtproto_downloader()
        except MtprotoConfigurationError as exc:
            raise _direct_download_setup_error(document, str(exc)) from exc
        except MtprotoDependencyError as exc:
            raise _direct_download_setup_error(document, str(exc)) from exc

        if status is not None:
            size = _human_file_size(getattr(document, "file_size", None))
            size_text = f" ({size})" if size else ""
            await status.update(
                f"⬇️ Preparing direct Telegram download{size_text}…",
                force=True,
            )

        async def report_progress(current, total):
            if status is not None:
                await status.update(
                    _direct_download_progress_text(
                        document,
                        current,
                        total,
                    )
                )

        try:
            await downloader.download(
                document.file_id,
                download_path.resolve(),
                report_progress if status is not None else None,
            )
        except (MtprotoConfigurationError, MtprotoDependencyError) as exc:
            raise _direct_download_setup_error(document, str(exc)) from exc
        except MtprotoError as exc:
            logger.warning(
                "MTProto download failed for Telegram file %s",
                getattr(document, "file_unique_id", "<unknown>"),
                exc_info=True,
            )
            raise PdfDownloadError(
                "❌ Could not download the selected PDF through Telegram's "
                "direct connection. Check TELEGRAM_API_ID and "
                "TELEGRAM_API_HASH, or reply to a freshly uploaded copy, then "
                "run /splitpdf again."
            ) from exc

    try:
        if _requires_mtproto_download(document):
            await download_with_mtproto()
        else:
            try:
                telegram_file = await context.bot.get_file(document.file_id)
                await telegram_file.download_to_drive(str(download_path))
            except TelegramError as exc:
                if "file is too big" not in str(exc).casefold():
                    raise
                try:
                    download_path.unlink(missing_ok=True)
                except OSError:
                    pass
                await download_with_mtproto()
        if download_path != file_path:
            download_path.replace(file_path)
    except PdfDownloadError:
        try:
            download_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except TelegramError as exc:
        try:
            download_path.unlink(missing_ok=True)
        except OSError:
            pass
        detail = str(exc)
        if "file is too big" in detail.casefold():
            detail = (
                "Telegram rejected this hosted Bot API download and the "
                "automatic large-file fallback did not complete."
            )
        raise PdfDownloadError(f"❌ Could not download the selected PDF: {detail}") from exc
    except OSError as exc:
        try:
            download_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise PdfDownloadError(f"❌ Could not save the selected PDF: {exc}") from exc
    return str(file_path)


async def _download_replied_pdf(update, context, status=None):
    message = getattr(update, "effective_message", None) or update.message
    document = _find_replied_pdf_document(message)
    if document is None:
        return None

    current_chat_id = update.effective_chat.id
    reference = _reply_message_reference(message, current_chat_id)
    source_chat_id, source_message_id = reference or (
        current_chat_id,
        update.message.message_id,
    )
    file_path = await _download_pdf_document(
        context,
        document,
        source_chat_id,
        source_message_id,
        status=status,
    )
    if source_chat_id == current_chat_id:
        _remember_chat_pdf(context, source_message_id, file_path)
    return file_path


async def _recover_replied_group_pdf(update, context, status=None):
    """Recover an inaccessible replied file via a temporary private forward."""
    message = getattr(update, "effective_message", None) or update.message
    reference = _reply_message_reference(message, update.effective_chat.id)
    if reference is None:
        return None
    source_chat_id, source_message_id = reference
    target_chat_id = update.effective_user.id
    forwarded = None

    try:
        forwarded = await context.bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
            disable_notification=True,
        )
        document = _find_replied_pdf_document(
            type(
                "ForwardWrapper",
                (),
                {"reply_to_message": forwarded, "external_reply": None},
            )()
        )
        if document is None:
            return None
        file_path = await _download_pdf_document(
            context,
            document,
            source_chat_id,
            source_message_id,
            status=status,
        )
        if source_chat_id == update.effective_chat.id:
            _remember_chat_pdf(context, source_message_id, file_path)
        return file_path
    except TelegramError:
        return None
    finally:
        if forwarded is not None:
            try:
                await context.bot.delete_message(
                    chat_id=target_chat_id,
                    message_id=forwarded.message_id,
                )
            except TelegramError:
                pass


def _format_elapsed_time(elapsed):
    seconds = max(0, int(elapsed))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _pdf_upload_status_text(
    label,
    transferred,
    total,
    elapsed,
    attempt,
    *,
    completed=False,
):
    total = max(int(total), 0)
    transferred = min(max(int(transferred), 0), total)
    percent = round((transferred / total) * 100) if total else 100
    if completed:
        heading = f"✅ Uploaded {label}."
    elif total and transferred >= total:
        heading = f"⬆️ Finishing {label} with Telegram…"
    else:
        heading = f"⬆️ Uploading {label}…"

    progress = (
        f"{_human_file_size(transferred)} / {_human_file_size(total)} "
        f"({percent}%)"
        if total
        else "Waiting for Telegram…"
    )
    attempt_text = (
        f"\nAttempt: {attempt}/{PDF_UPLOAD_ATTEMPTS}"
        if attempt > 1
        else ""
    )
    return (
        f"{heading}\n"
        f"Progress: {progress}\n"
        f"Elapsed: {_format_elapsed_time(elapsed)}"
        f"{attempt_text}"
    )


async def _upload_progress_ticker(
    status,
    tracked_file,
    label,
    attempt,
    started_at,
    finished,
):
    loop = asyncio.get_running_loop()
    while not finished.is_set():
        try:
            await asyncio.wait_for(
                finished.wait(),
                timeout=PDF_UPLOAD_STATUS_INTERVAL,
            )
        except asyncio.TimeoutError:
            await status.update(
                _pdf_upload_status_text(
                    label,
                    tracked_file.transferred,
                    tracked_file.total_bytes,
                    loop.time() - started_at,
                    attempt,
                )
            )


async def _send_pdf_with_retry(
    message,
    output_path,
    label,
    *,
    status=None,
    reply_to_message_id=None,
    caption=None,
):
    """Stream a PDF with byte progress and transient-network retries."""
    output_path = Path(output_path)
    total_bytes = output_path.stat().st_size
    for attempt in range(1, PDF_UPLOAD_ATTEMPTS + 1):
        try:
            # Reopen on every attempt because a failed upload may consume the
            # previous file object's stream.
            with output_path.open("rb") as pdf:
                tracked_file = _UploadProgressFile(pdf, total_bytes)
                document = InputFile(
                    tracked_file,
                    filename=output_path.name,
                    read_file_handle=False,
                )
                loop = asyncio.get_running_loop()
                started_at = loop.time()
                finished = asyncio.Event()
                ticker = None
                if status is not None:
                    await status.update(
                        _pdf_upload_status_text(
                            label,
                            0,
                            total_bytes,
                            0,
                            attempt,
                        ),
                        force=True,
                    )
                    ticker = asyncio.create_task(
                        _upload_progress_ticker(
                            status,
                            tracked_file,
                            label,
                            attempt,
                            started_at,
                            finished,
                        )
                    )
                try:
                    reply_kwargs = {}
                    if reply_to_message_id is not None:
                        reply_kwargs = {
                            "reply_to_message_id": reply_to_message_id,
                            "allow_sending_without_reply": True,
                        }
                    await message.reply_document(
                        document=document,
                        caption=caption or label,
                        read_timeout=PDF_UPLOAD_READ_TIMEOUT,
                        write_timeout=PDF_UPLOAD_WRITE_TIMEOUT,
                        connect_timeout=PDF_UPLOAD_CONNECT_TIMEOUT,
                        pool_timeout=PDF_UPLOAD_POOL_TIMEOUT,
                        **reply_kwargs,
                    )
                finally:
                    finished.set()
                    if ticker is not None:
                        await asyncio.gather(ticker, return_exceptions=True)

                if status is not None:
                    await status.update(
                        _pdf_upload_status_text(
                            label,
                            total_bytes,
                            total_bytes,
                            loop.time() - started_at,
                            attempt,
                            completed=True,
                        ),
                        force=True,
                    )
            return
        except RetryAfter as exc:
            if attempt == PDF_UPLOAD_ATTEMPTS:
                raise
            retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds()
                if hasattr(retry_after, "total_seconds")
                else float(retry_after)
            )
            delay += 1
            if status is not None:
                await status.update(
                    f"⏳ Telegram paused the {label} upload. "
                    f"Retrying attempt {attempt + 1}/{PDF_UPLOAD_ATTEMPTS} "
                    f"in {delay:.0f}s…",
                    force=True,
                )
            await asyncio.sleep(delay)
        except NetworkError:
            if attempt == PDF_UPLOAD_ATTEMPTS:
                raise
            delay = 2 ** (attempt - 1)
            if status is not None:
                await status.update(
                    f"🔄 The {label} upload was interrupted. "
                    f"Retrying attempt {attempt + 1}/{PDF_UPLOAD_ATTEMPTS} "
                    f"in {delay}s…",
                    force=True,
                )
            await asyncio.sleep(delay)


def _pdf_split_process(input_path, duplex, output_dir, events):
    """Process entry point; PyMuPDF can deadlock in Python worker threads."""
    try:
        last_percent = -1

        def report(processed, total):
            nonlocal last_percent
            percent = (processed * 100) // total if total else 0
            if processed not in (0, total) and percent <= last_percent:
                return
            last_percent = percent
            events.put(("progress", processed, total))

        result = split_for_manual_color(
            input_path,
            duplex,
            output_dir,
            report,
        )
        events.put(("result", result))
    except Exception as exc:
        events.put(("error", type(exc).__name__, str(exc)))


def _pdf_worker_exception(name, detail):
    if name == "FileNotFoundError":
        return FileNotFoundError(detail)
    if name == "ValueError":
        return ValueError(detail)
    if name == "FileDataError":
        return fitz.FileDataError(detail)
    return RuntimeError(f"PDF worker failed ({name}): {detail}")


async def _split_pdf_with_progress(
    input_path,
    duplex,
    output_dir,
    status,
):
    """Run PyMuPDF in a process while asynchronously reporting progress."""
    # This bot runs on Linux. A separate process avoids PyMuPDF's worker-thread
    # deadlock while keeping the asyncio event loop responsive.
    process_context = multiprocessing.get_context("fork")
    events = process_context.Queue()
    worker = process_context.Process(
        target=_pdf_split_process,
        args=(
            input_path,
            duplex,
            output_dir,
            events,
        ),
        daemon=True,
    )
    worker.start()

    try:
        while True:
            event = None
            try:
                event = events.get_nowait()
                while event[0] == "progress":
                    latest_progress = event
                    try:
                        event = events.get_nowait()
                    except queue.Empty:
                        event = latest_progress
                        break
            except queue.Empty:
                pass

            if event is not None:
                kind = event[0]
                if kind == "result":
                    return event[1]
                if kind == "error":
                    raise _pdf_worker_exception(event[1], event[2])
                if kind == "progress":
                    _, processed, total = event
                    percent = round((processed / total) * 100) if total else 0
                    await status.update(
                        f"⚙️ Splitting PDF: {processed}/{total} pages "
                        f"({percent}%)…"
                    )

            if not worker.is_alive():
                # Queue feeder delivery can lag process exit very briefly.
                # Drain with a bounded grace period before treating it as a
                # crashed worker.
                loop = asyncio.get_running_loop()
                grace_deadline = loop.time() + 0.5
                while loop.time() < grace_deadline:
                    try:
                        final_event = events.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.05)
                        continue
                    if final_event[0] == "result":
                        return final_event[1]
                    if final_event[0] == "error":
                        raise _pdf_worker_exception(
                            final_event[1],
                            final_event[2],
                        )
                raise RuntimeError(
                    f"PDF worker stopped unexpectedly (exit {worker.exitcode})."
                )
            await asyncio.sleep(0.1)
    finally:
        if worker.is_alive():
            await asyncio.to_thread(worker.join, 1)
        if worker.is_alive():
            worker.terminate()
            await asyncio.to_thread(worker.join, 1)
        worker.close()
        events.close()


def _pdf_source_name(source):
    return Path(
        getattr(source.document, "file_name", None)
        or f"document-{source.message_id}.pdf"
    ).name


async def _split_replied_media_group(
    update,
    context,
    selection,
    split_args,
    batch_status,
):
    """Download, split, and upload selected album PDFs strictly in order."""
    sources = selection.sources
    total = len(sources)
    succeeded = 0
    discovery_text = (
        f"📚 Found {total} {'PDF' if total == 1 else 'PDFs'} in the "
        "selected album. "
        "Processing them one by one…"
    )
    if not selection.verified_complete:
        discovery_text += (
            "\n⚠️ Direct album verification was unavailable, so these are "
            "the PDF files observed by the bot."
        )
    await batch_status.update(
        discovery_text,
        force=True,
    )

    for index, source in enumerate(sources, start=1):
        file_name = _pdf_source_name(source)
        prefix = f"📄 PDFs: {index}/{total} — {file_name}"
        reply_to_message_id = (
            source.message_id
            if source.chat_id == update.effective_chat.id
            else None
        )
        batch_status.set_prefix(prefix)
        try:
            await batch_status.update(
                _download_status_text(source.document),
                force=True,
            )
            try:
                file_path = await _download_pdf_document(
                    context,
                    source.document,
                    source.chat_id,
                    source.message_id,
                    status=batch_status,
                )
            except PdfDownloadError as exc:
                await batch_status.update(str(exc), force=True)
                continue

            if source.chat_id == update.effective_chat.id:
                _remember_chat_pdf(context, source.message_id, file_path)
            if await split_pdf(
                update,
                context,
                args=split_args,
                input_path=file_path,
                status=batch_status,
                display_name=file_name,
                reply_to_message_id=reply_to_message_id,
                announce_completion=False,
                return_result=True,
            ):
                succeeded += 1
        except Exception as exc:
            logger.exception(
                "Unexpected failure while processing album PDF message %s",
                source.message_id,
            )
            await batch_status.update(
                f"❌ Unexpected PDF processing error: {exc}",
                force=True,
            )
        finally:
            batch_status.set_prefix(None)

    failed = total - succeeded
    noun = "PDF" if total == 1 else "PDFs"
    if failed:
        final_text = (
            f"⚠️ Album processing finished: {succeeded}/{total} {noun} "
            f"completed, {failed} failed."
        )
    elif selection.verified_complete:
        final_text = (
            f"✅ All PDFs have been processed: {succeeded}/{total} completed."
        )
    else:
        final_text = (
            f"✅ Cached PDFs have been processed: {succeeded}/{total} "
            "completed."
        )
    if not selection.verified_complete:
        final_text += (
            "\n⚠️ Telegram could not verify album completeness; only cached "
            "PDF files were processed."
        )
    try:
        await update.message.reply_text(
            final_text,
            reply_to_message_id=update.message.message_id,
            allow_sending_without_reply=True,
        )
    except TelegramError:
        logger.warning(
            "Could not send the final PDF album summary",
            exc_info=True,
        )
        # The original tracker is above the uploaded documents, but retaining
        # the aggregate result there is still better than losing it entirely.
        await batch_status.update(final_text, force=True)
    else:
        tracker_text = (
            "⚠️ Finished processing PDFs with failures."
            if failed
            else "✅ Finished processing PDFs."
        )
        await batch_status.update(tracker_text, force=True)
    return failed == 0


async def split_pdf(
    update: Update,
    context: CallbackContext,
    args=None,
    input_path=None,
    status=None,
    display_name=None,
    reply_to_message_id=None,
    announce_completion=True,
    return_result=False,
):
    """Split a selected PDF; optionally return success to internal callers."""
    def finish(result):
        # PTB ConversationHandler treats bool as integer conversation states.
        # Telegram entry points must therefore return None, while the internal
        # album loop still needs a boolean to count per-file successes.
        return result if return_result else None

    if not is_user_authorized(update.message.from_user):
        await update.message.reply_text("❌ You are not authorized to split files.")
        return finish(False)

    split_args = context.args if args is None else args
    try:
        duplex = _duplex_from_args(split_args)
    except ValueError as exc:
        if status is None:
            await update.message.reply_text(str(exc))
        else:
            await status.update(str(exc), force=True)
        return finish(False)

    message = getattr(update, "effective_message", None) or update.message
    current_chat_id = update.effective_chat.id
    explicit_reply = _has_explicit_reply(message)

    if input_path is None:
        album_reference = _replied_media_group_reference(
            message,
            current_chat_id,
        )
        if album_reference is not None:
            if status is None:
                source_chat_id, source_message_id, _ = album_reference
                source_reply_id = (
                    source_message_id
                    if source_chat_id == current_chat_id
                    else None
                )
                status = await _new_pdf_status(
                    update.message,
                    context,
                    "🔎 Locating PDFs in the selected album…",
                    reply_to_message_id=source_reply_id,
                )
            try:
                selection = await _resolve_replied_media_group_sources(
                    update,
                    context,
                )
            except PdfDownloadError as exc:
                await status.update(str(exc), force=True)
                return finish(False)
            return finish(
                await _split_replied_media_group(
                    update,
                    context,
                    selection,
                    split_args,
                    status,
                )
            )

    if status is None:
        status = await _new_pdf_status(
            update.message,
            context,
            "🔎 Locating the selected PDF…",
        )

    try:
        if input_path is None:
            replied_document = _find_replied_pdf_document(message)
            if replied_document is not None:
                await status.update(
                    _download_status_text(replied_document),
                    force=True,
                )
            input_path = await _download_replied_pdf(
                update,
                context,
                status=status,
            )
    except PdfDownloadError as exc:
        await status.update(str(exc), force=True)
        return finish(False)
    if input_path is None and explicit_reply:
        input_path = _cached_replied_pdf(
            message,
            current_chat_id,
            context,
        )
    if input_path is None and explicit_reply:
        await status.update(
            "⬇️ Recovering and downloading the selected group PDF…",
            force=True,
        )
        try:
            input_path = await _recover_replied_group_pdf(
                update,
                context,
                status=status,
            )
        except PdfDownloadError as exc:
            await status.update(str(exc), force=True)
            return finish(False)
    if not input_path:
        pending_args = ["--duplex"] if duplex else []
        prompt_text = (
            "No downloadable PDF was selected. If you replied to a PDF, "
            "Telegram may have hidden it because group privacy is enabled.\n\n"
            "Reply directly to this bot message with the PDF file. I will "
            f"split it automatically in {'duplex' if duplex else 'simplex'} "
            "mode.\n\n"
            "You can also send the PDF with /splitpdf as its caption."
        )
        await status.update(prompt_text, force=True)
        context.user_data["pending_pdf_split"] = {
            "args": pending_args,
            "chat_id": update.effective_chat.id,
            "prompt_message_id": status.message_id,
        }
        return finish(False)
    if Path(input_path).suffix.lower() != ".pdf":
        await status.update("❌ The selected file is not a PDF.", force=True)
        return finish(False)

    await status.update(
        f"⚙️ Splitting PDF in {'duplex' if duplex else 'simplex'} mode…",
        force=True,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-pdf-") as output_dir:
            result = await _split_pdf_with_progress(
                input_path,
                duplex,
                output_dir,
                status,
            )

            for label, output_path in (
                ("B&W pages", result.bw_path),
                ("Color pages", result.color_path),
            ):
                if output_path:
                    output_caption = (
                        f"{display_name} — {label}"
                        if display_name
                        else label
                    )
                    await _send_pdf_with_retry(
                        update.message,
                        output_path,
                        label,
                        status=status,
                        reply_to_message_id=reply_to_message_id,
                        caption=output_caption,
                    )
            if announce_completion:
                await status.update(
                    _split_completion_text(result, duplex),
                    force=True,
                )
            return finish(True)
    except (FileNotFoundError, ValueError, fitz.FileDataError) as exc:
        await status.update(f"❌ Could not split PDF: {exc}", force=True)
        return finish(False)
    except TelegramError as exc:
        await status.update(
            f"❌ Telegram could not send the result: {exc}",
            force=True,
        )
        return finish(False)
    except Exception as exc:
        await status.update(
            f"❌ Unexpected PDF processing error: {exc}",
            force=True,
        )
        return finish(False)
