import socket
import requests
from telegram import Update
from telegram import BotCommand
import platform
import shutil
import psutil
import GPUtil

from bot.utils import is_user_authorized

async def set_bot_menu(application):
    commands = [
        BotCommand("run", "Run a command"),
        BotCommand("get_local_ip", "Get your local IP address"),
        BotCommand("get_public_ip", "Get your public IP address"),
        BotCommand("get_system_info", "Get system information"),
        BotCommand("get_system_usage", "Get system usage"),
        BotCommand("get_disk_usage", "Get disk usage")
    ]
    await application.bot.set_my_commands(commands)

async def get_local_ip(update: Update, context) -> None:
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        await update.message.reply_text(f'Your local IP is: {local_ip}')
    except Exception as e:
        await update.message.reply_text(f'Error retrieving local IP: {str(e)}')

async def get_system_info(update: Update, context) -> None:
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return
    
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
        await update.message.reply_text(response)
    except Exception as e:
        await update.message.reply_text(f'Error retrieving system info: {str(e)}')

async def get_disk_usage(update: Update, context) -> None:
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return
    
    try:
        total, used, free = shutil.disk_usage("/")
        response = (
            f"Total: {total // (2**30)} GiB\n"
            f"Used: {used // (2**30)} GiB\n"
            f"Free: {free // (2**30)} GiB"
        )
        await update.message.reply_text(response)
    except Exception as e:
        await update.message.reply_text(f'Error retrieving disk usage: {str(e)}')

async def get_public_ip(update: Update, context) -> None:
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return
    
    try:
        response = requests.get('https://api.ipify.org?format=json')
        public_ip = response.json().get('ip')
        await update.message.reply_text(f'Your public IP is: {public_ip}')
    except Exception as e:
        await update.message.reply_text(f'Error retrieving public IP: {str(e)}')

async def get_system_usage(update: Update, context) -> None:
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return
    
    try:
        # Get CPU usage and count
        cpu_usage = round(psutil.cpu_percent(interval=1), 1)
        cpu_count = psutil.cpu_count(logical=True)
        
        # Get RAM usage and total
        memory = psutil.virtual_memory()
        ram_usage = round(memory.percent, 1)
        ram_used = round(memory.used / (2**30), 1) # Convert bytes to GiB 
        ram_total = round(memory.total // (2**30), 1)  # Convert bytes to GiB
        
        # Get GPU usage and total memory (if available)
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu = gpus[0]
                gpu_usage = round(gpu.load * 100, 1)
                gpu_used_memory = gpu.memoryUsed
                gpu_total_memory = gpu.memoryTotal
            else:
                gpu_usage = 'N/A'
                gpu_used_memory = 'N/A'
                gpu_total_memory = 'N/A'
        except ImportError:
            gpu_usage = 'GPUtil not installed'
            gpu_used_memory = 'N/A'
            gpu_total_memory = 'N/A'
        
        response = (
            f"CPU Usage: {cpu_usage}% ({cpu_count} Cores)\n"
            f"RAM Usage: {ram_usage}% ({ram_used} GiB/{ram_total} GiB)\n"
            f"GPU Usage: {gpu_usage if isinstance(gpu_usage, str) else f'{gpu_usage}%'} "
            f"({gpu_used_memory if isinstance(gpu_used_memory, str) else f'{gpu_used_memory} MiB'}/"
            f"{gpu_total_memory if isinstance(gpu_total_memory, str) else f'{gpu_total_memory} MiB'})"
        )
        await update.message.reply_text(response)
    except Exception as e:
        await update.message.reply_text(f'Error retrieving system usage: {str(e)}')
