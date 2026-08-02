import sqlite3
import subprocess
import asyncio
import shlex
import tempfile
from dataclasses import dataclass
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, CallbackContext
from telegram.error import NetworkError, RetryAfter, TelegramError
import re
from pathlib import Path

import fitz
from PIL import Image, ImageChops

from bot.config import SUPER_ADMIN_USERNAME

UPLOAD_DIR = "uploads"
DB_PATH = "db/authorized_users.db"
PDF_UPLOAD_ATTEMPTS = 3
PDF_UPLOAD_READ_TIMEOUT = 120
PDF_UPLOAD_WRITE_TIMEOUT = 300
PDF_UPLOAD_CONNECT_TIMEOUT = 30
PDF_UPLOAD_POOL_TIMEOUT = 30


@dataclass(frozen=True)
class SplitResult:
    bw_path: Optional[Path]
    color_path: Optional[Path]
    bw_pages: int
    color_pages: int
    guide: str


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


def split_for_manual_color(input_path, duplex=False, output_dir=None):
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
    return get_user_role(user_or_id) is not None

def is_admin(user_or_id) -> bool:
    return get_user_role(user_or_id) == "admin"


def is_super_admin(user) -> bool:
    """Allow host-level operations only for the configured primary admin."""
    username = (getattr(user, "username", None) or "").casefold()
    return (
        bool(SUPER_ADMIN_USERNAME)
        and username == SUPER_ADMIN_USERNAME
        and get_user_role(user) == "admin"
    )


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
    admin = update.message.from_user

    if not is_admin(admin):
        await update.message.reply_text("❌ You are not authorized to remove users.")
        return

    if len(context.args) == 0 and not update.message.reply_to_message:
        await update.message.reply_text("Usage: /remove <username> or reply to a user's message.")
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

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if user_id_to_remove is not None:
        cursor.execute(
            "DELETE FROM authorized_users WHERE user_id = ?",
            (user_id_to_remove,),
        )
    else:
        cursor.execute(
            "DELETE FROM authorized_users WHERE username = ? COLLATE NOCASE",
            (username,),
        )
    removed = cursor.rowcount
    conn.commit()
    conn.close()

    if removed:
        display_name = f"@{username}" if username else f"ID {user_id_to_remove}"
        await update.message.reply_text(f"✅ User {display_name} has been removed.")
    else:
        await update.message.reply_text("❌ Authorized user not found.")

async def handle_file_upload(update: Update, context: CallbackContext) -> None:
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

    file_id = document.file_id
    file = await context.bot.get_file(file_id)

    file_path = _upload_file_path(
        update.effective_chat.id,
        update.message.message_id,
        document.file_name or "upload",
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Download the file
    await file.download_to_drive(str(file_path))
    if _is_pdf_document(document):
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
            input_path=str(file_path),
        )
        return

    if is_pending_reply:
        context.user_data.pop("pending_pdf_split", None)
        await split_pdf(
            update,
            context,
            args=pending_split["args"],
            input_path=str(file_path),
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
        "Upload a PDF first, reply to a PDF, or use the command as its caption."
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


def _find_replied_pdf_document(message):
    """Find a PDF in normal, quoted, or cross-chat Telegram replies."""
    if message is None:
        return None

    replied = getattr(message, "reply_to_message", None)
    candidates = [replied]
    if replied is not None:
        candidates.append(getattr(replied, "external_reply", None))
        candidates.append(getattr(replied, "reply_to_message", None))
    candidates.append(getattr(message, "external_reply", None))

    for candidate in candidates:
        if candidate is None:
            continue
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
    if message is None:
        return None

    replied = getattr(message, "reply_to_message", None)
    candidates = [replied]
    if replied is not None:
        candidates.append(getattr(replied, "external_reply", None))
        candidates.append(getattr(replied, "reply_to_message", None))
    candidates.append(getattr(message, "external_reply", None))

    for candidate in candidates:
        if candidate is None:
            continue
        message_id = getattr(candidate, "message_id", None)
        if message_id is None:
            continue
        chat = getattr(candidate, "chat", None)
        chat_id = getattr(chat, "id", None) or current_chat_id
        return chat_id, message_id
    return None


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
):
    file_path = _upload_file_path(
        source_chat_id,
        source_message_id,
        document.file_name or "upload.pdf",
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)
    telegram_file = await context.bot.get_file(document.file_id)
    await telegram_file.download_to_drive(str(file_path))
    return str(file_path)


async def _download_replied_pdf(update, context):
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
    )
    if source_chat_id == current_chat_id:
        _remember_chat_pdf(context, source_message_id, file_path)
    return file_path


async def _recover_replied_group_pdf(update, context):
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


async def _send_pdf_with_retry(message, output_path, label):
    """Upload a PDF with long media timeouts and transient-network retries."""
    for attempt in range(1, PDF_UPLOAD_ATTEMPTS + 1):
        try:
            # Reopen on every attempt because a failed upload may consume the
            # previous file object's stream.
            with output_path.open("rb") as pdf:
                await message.reply_document(
                    document=pdf,
                    filename=output_path.name,
                    caption=label,
                    read_timeout=PDF_UPLOAD_READ_TIMEOUT,
                    write_timeout=PDF_UPLOAD_WRITE_TIMEOUT,
                    connect_timeout=PDF_UPLOAD_CONNECT_TIMEOUT,
                    pool_timeout=PDF_UPLOAD_POOL_TIMEOUT,
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
            await asyncio.sleep(delay + 1)
        except NetworkError:
            if attempt == PDF_UPLOAD_ATTEMPTS:
                raise
            await asyncio.sleep(2 ** (attempt - 1))


async def split_pdf(update: Update, context: CallbackContext, args=None, input_path=None):
    """Split the latest/replied PDF and send all generated files to Telegram."""
    if not is_user_authorized(update.message.from_user):
        await update.message.reply_text("❌ You are not authorized to split files.")
        return

    try:
        duplex = _duplex_from_args(context.args if args is None else args)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    message = getattr(update, "effective_message", None) or update.message
    current_chat_id = update.effective_chat.id
    explicit_reply = _has_explicit_reply(message)

    if input_path is None:
        input_path = await _download_replied_pdf(update, context)
    if input_path is None and explicit_reply:
        input_path = _cached_replied_pdf(
            message,
            current_chat_id,
            context,
        )
    if input_path is None and explicit_reply:
        input_path = await _recover_replied_group_pdf(update, context)
    if not input_path:
        pending_args = ["--duplex"] if duplex else []
        prompt = await update.message.reply_text(
            "No downloadable PDF was selected. If you replied to a PDF, "
            "Telegram may have hidden it because group privacy is enabled.\n\n"
            "Reply directly to this bot message with the PDF file. I will "
            f"split it automatically in {'duplex' if duplex else 'simplex'} "
            "mode.\n\n"
            "You can also send the PDF with /splitpdf as its caption."
        )
        context.user_data["pending_pdf_split"] = {
            "args": pending_args,
            "chat_id": update.effective_chat.id,
            "prompt_message_id": prompt.message_id,
        }
        return
    if Path(input_path).suffix.lower() != ".pdf":
        await update.message.reply_text("❌ The selected file is not a PDF.")
        return

    status = await update.message.reply_text(
        f"Splitting PDF in {'duplex' if duplex else 'simplex'} mode…"
    )

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-pdf-") as output_dir:
            result = await asyncio.to_thread(
                split_for_manual_color,
                input_path,
                duplex,
                output_dir,
            )

            await status.edit_text(result.guide[:4096])
            for label, output_path in (
                ("B&W pages", result.bw_path),
                ("Color pages", result.color_path),
            ):
                if output_path:
                    await _send_pdf_with_retry(
                        update.message,
                        output_path,
                        label,
                    )
    except (FileNotFoundError, ValueError, fitz.FileDataError) as exc:
        await status.edit_text(f"❌ Could not split PDF: {exc}")
    except TelegramError as exc:
        await status.edit_text(f"❌ Telegram could not send the result: {exc}")
    except Exception as exc:
        await status.edit_text(f"❌ Unexpected PDF processing error: {exc}")

def get_ram_info():
    try:
        result = subprocess.run(["sudo", "dmidecode", "--type", "17"], capture_output=True, text=True)
        output = result.stdout

        ram_info = []
        for ram_block in output.split("\n\n"):
            manufacturer = re.search(r"Manufacturer:\s+(.+)", ram_block)
            speed = re.search(r"Speed:\s+(.+)", ram_block)
            ram_type = re.search(r"Type:\s+(.+)", ram_block)
            form_factor = re.search(r"Form Factor:\s+(.+)", ram_block)
            size = re.search(r"Size:\s+(.+)", ram_block)

            if size and "No Module Installed" not in size.group(1):  # Ignore empty slots
                ram_info.append({
                    "Manufacturer": manufacturer.group(1) if manufacturer else "Unknown",
                    "Speed": speed.group(1) if speed else "Unknown",
                    "Type": ram_type.group(1) if ram_type else "Unknown",
                    "Form Factor": form_factor.group(1) if form_factor else "Unknown",
                    "Size": size.group(1)
                })

        return ram_info
    except Exception as e:
        return [f"Error retrieving RAM info: {str(e)}"]
