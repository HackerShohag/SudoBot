import asyncio
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import RetryAfter
from bot.keyboard import update_command_history, update_keyboard
from bot.utils import is_user_authorized
from bot.config import MAX_CHARS
import signal

AWAITING_SUDO_PASSWORD = 1
running_process = None  # Global variable to store the running process

async def execute_command(command: str, update, context, reply_to_message_id):
    global running_process
    running_process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    update_command_history(update, context)

    message = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text="Running command...",
        reply_to_message_id=reply_to_message_id
    )

    output_lines = []
    max_chars = 4000
    last_edit_time = asyncio.get_event_loop().time()  # Track last edit time

    async for line in running_process.stdout:
        output_lines.append(line.decode().strip())
        output_text = "\n".join(output_lines[-20:])  # Last 20 lines

        if len(output_text) > max_chars:
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="Output too long! Sending full output in separate messages...",
                reply_to_message_id=reply_to_message_id
            )
            await send_large_output(update, context, output_lines, reply_to_message_id)
            return

        new_text = f"```\n{output_text}\n```"

        # Limit message edits to avoid flood control
        if message.text != new_text and (asyncio.get_event_loop().time() - last_edit_time > 3):  # Edit every 3 seconds
            try:
                await context.bot.edit_message_text(
                    chat_id=update.message.chat_id,
                    message_id=message.message_id,
                    text=new_text,
                    parse_mode='MarkdownV2'
                )
                last_edit_time = asyncio.get_event_loop().time()  # Update last edit time
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)  # Wait if Telegram blocks edits

    err = await running_process.stderr.read()
    if err:
        output_lines.append(f"\nError:\n{err.decode().strip()}")
        await send_large_output(update, context, output_lines, reply_to_message_id)

    await running_process.wait()
    running_process = None

    await update_keyboard(update, context)

async def send_large_output(update: Update, context: ContextTypes.DEFAULT_TYPE, output_lines, reply_to_message_id):
    """Splits large command output into multiple messages to prevent errors."""
    chunk = "```\n"

    for line in output_lines:
        if len(chunk) + len(line) > int(MAX_CHARS) - 4:  # Adjust for the length of the backticks
            chunk += "```"
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text=chunk,
                reply_to_message_id=reply_to_message_id,
                parse_mode='MarkdownV2'
            )
            chunk = f"```\n{line}\n"
        else:
            chunk += line + "\n"

    if chunk.strip() != "```":
        chunk += "```"
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=chunk,
            reply_to_message_id=reply_to_message_id,
            parse_mode='MarkdownV2'
        )

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
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
    global running_process
    if running_process and running_process.returncode is None:
        running_process.send_signal(signal.SIGTERM)  # Send SIGTERM instead of terminate()
        try:
            await asyncio.wait_for(running_process.wait(), timeout=5)  # Give it time to exit
        except asyncio.TimeoutError:
            running_process.kill()  # Force kill if it does not stop
        running_process = None  # Reset process reference
        await update.message.reply_text("✅ Command execution stopped.")
        await update_keyboard(update, context)
    else:
        await update.message.reply_text("⚠️ No command is currently running.")