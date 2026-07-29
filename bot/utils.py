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
import os
import re
from pathlib import Path

import fitz
from PIL import Image, ImageChops

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

def get_user_role(user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM authorized_users WHERE user_id = ?", (user_id,))
    role = cursor.fetchone()
    conn.close()
    return role[0] if role else None

def is_user_authorized(user_id: int) -> bool:
    return get_user_role(user_id) is not None

def is_admin(user_id: int) -> bool:
    return get_user_role(user_id) == "admin"


async def authorize_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to add users.")
        return

    if len(context.args) == 0 and not update.message.reply_to_message:
        await update.message.reply_text("Usage: /authorize <username> [role(admin/user)] or reply to a user's message.")
        return

    if len(context.args) > 0:
        username = context.args[0].lstrip('@')
        role = context.args[1].lower() if len(context.args) > 1 else 'user'
        full_name = update.message.reply_to_message.from_user.full_name if update.message.reply_to_message else update.message.from_user.full_name
        user_id_to_add = update.message.reply_to_message.from_user.id if update.message.reply_to_message else None
    else:
        username = update.message.reply_to_message.from_user.username.lstrip('@')
        full_name = update.message.reply_to_message.from_user.full_name
        role = 'user'
        user_id_to_add = update.message.reply_to_message.from_user.id

    if role not in ["admin", "user"]:
        await update.message.reply_text("❌ Invalid role. Use 'admin' or 'user'.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO authorized_users (username, user_id, full_name, added_by, role) VALUES (?, ?, ?, ?, ?)",
            (username, user_id_to_add, full_name, update.message.from_user.username, role),
        )
        conn.commit()
        await update.message.reply_text(f"✅ User {username} authorized as {role}.")
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ User is already authorized.")
    finally:
        conn.close()

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to remove users.")
        return

    if len(context.args) == 0 and not update.message.reply_to_message:
        await update.message.reply_text("Usage: /remove <username> or reply to a user's message.")
        return

    if len(context.args) > 0:
        username = context.args[0].lstrip('@')
    else:
        username = update.message.reply_to_message.from_user.username.lstrip('@')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM authorized_users WHERE username = ?", (username,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ User {username} has been removed.")

async def handle_file_upload(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to upload files.")
        return

    document = update.message.document
    file_id = document.file_id
    file = await context.bot.get_file(file_id)

    # Ensure the upload directory exists
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    safe_name = Path(document.file_name or "upload").name
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    
    # Download the file
    await file.download_to_drive(file_path)

    context.user_data["last_uploaded_file"] = file_path

    caption_args = _splitpdf_caption_args(update.message.caption)
    if caption_args is not None:
        await split_pdf(update, context, args=caption_args, input_path=file_path)
        return

    suffix = (
        "\nUse /splitpdf or /splitpdf --duplex to split this PDF."
        if safe_name.lower().endswith(".pdf")
        else ""
    )
    await update.message.reply_text(f"File uploaded successfully: {file_path}{suffix}")


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


async def _download_replied_pdf(update, context):
    replied = update.message.reply_to_message
    document = replied.document if replied else None
    if not document:
        return None

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_name = Path(document.file_name or "upload.pdf").name
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    telegram_file = await context.bot.get_file(document.file_id)
    await telegram_file.download_to_drive(file_path)
    context.user_data["last_uploaded_file"] = file_path
    return file_path


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
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to split files.")
        return

    try:
        duplex = _duplex_from_args(context.args if args is None else args)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    if input_path is None:
        input_path = await _download_replied_pdf(update, context)
    if input_path is None:
        input_path = context.user_data.get("last_uploaded_file")
    if not input_path:
        await update.message.reply_text(
            "Upload a PDF first, or reply to a PDF with /splitpdf [--duplex]."
        )
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
