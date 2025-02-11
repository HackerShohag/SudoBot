# bot_menu.py
import asyncio
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from bot.keyboard import update_command_history, update_keyboard
from bot.config import MAX_CHARS

AWAITING_SUDO_PASSWORD = 1

# Modify the execute_command function to update the command counter and last run command
async def execute_command(command: str, update, context, reply_to_message_id):
    process = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    # Update command frequency counter and last run command
    update_command_history(update, context)

    # Reply to the original message that sent the command
    message = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text="Running command...",
        reply_to_message_id=reply_to_message_id
    )

    output_lines = []
    max_chars = 4000  # Prevent Telegram message limit error

    async for line in process.stdout:
        output_lines.append(line.decode().strip())
        output_text = "\n".join(output_lines[-20:])  # Keep last 20 lines

        if len(output_text) > max_chars:
            await context.bot.edit_message_text(
                chat_id=update.message.chat_id,
                message_id=message.message_id,
                text="Output too long! Sending full output in separate messages..."
            )
            await send_large_output(update, context, output_lines, reply_to_message_id)
            return

        await context.bot.edit_message_text(
            chat_id=update.message.chat_id,
            message_id=message.message_id,
            text=f"```\n{output_text}\n```",
            parse_mode='MarkdownV2'
        )

    err = await process.stderr.read()
    if err:
        output_lines.append(f"\nError:\n{err.decode().strip()}")
        await send_large_output(update, context, output_lines, reply_to_message_id)

    await process.wait()

    # Update the keyboard with frequent and last commands
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
    command = ' '.join(context.args)
    
    if not command:
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
