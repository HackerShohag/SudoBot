import socket
import requests
from telegram import Update
from telegram import BotCommand
import platform
import shutil

async def set_bot_menu(application):
    commands = [
        BotCommand("run", "Run a command"),
        BotCommand("get_local_ip", "Get your local IP address"),
        BotCommand("get_public_ip", "Get your public IP address"),
        BotCommand("get_system_info", "Get system information"),
        BotCommand("get_disk_usage", "Get disk usage")
    ]
    await application.bot.set_my_commands(commands)

def run(update: Update, context) -> None:
    update.message.reply_text('Please complete the run command by providing arguments.')

def get_local_ip(update: Update, context) -> None:
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        update.message.reply_text(f'Your local IP is: {local_ip}')
    except Exception as e:
        update.message.reply_text(f'Error retrieving local IP: {str(e)}')

def get_system_info(update: Update, context) -> None:
    print('get_system_info')
    try:
        system_info = platform.uname()
        response = (
            f"System: {system_info.system}\n"
            f"Node Name: {system_info.node}\n"
            f"Release: {system_info.release}\n"
            f"Version: {system_info.version}\n"
            f"Machine: {system_info.machine}\n"
            f"Processor: {system_info.processor}"
        )
        update.message.reply_text(response)
    except Exception as e:
        update.message.reply_text(f'Error retrieving system info: {str(e)}')

def get_disk_usage(update: Update, context) -> None:
    try:
        total, used, free = shutil.disk_usage("/")
        response = (
            f"Total: {total // (2**30)} GiB\n"
            f"Used: {used // (2**30)} GiB\n"
            f"Free: {free // (2**30)} GiB"
        )
        update.message.reply_text(response)
    except Exception as e:
        update.message.reply_text(f'Error retrieving disk usage: {str(e)}')

def get_public_ip(update: Update, context) -> None:
    try:
        response = requests.get('https://api.ipify.org?format=json')
        public_ip = response.json().get('ip')
        update.message.reply_text(f'Your public IP is: {public_ip}')
    except Exception as e:
        update.message.reply_text(f'Error retrieving public IP: {str(e)}')