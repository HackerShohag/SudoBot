import sqlite3
import subprocess
from telegram import Update
from telegram.ext import ContextTypes, CallbackContext
import os
import re

UPLOAD_DIR = "uploads"
DB_PATH = "db/authorized_users.db"

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
    document = update.message.document
    file_id = document.file_id
    file = await context.bot.get_file(file_id)

    # Ensure the upload directory exists
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    file_path = os.path.join(UPLOAD_DIR, document.file_name)
    
    # Download the file
    await file.download_to_drive(file_path)

    await update.message.reply_text(f"File uploaded successfully: `{file_path}`", parse_mode="MarkdownV2")

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
