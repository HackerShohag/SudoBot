import re
import socket
import requests
from telegram import Update
from telegram import BotCommand
import platform
import shutil
import psutil
import GPUtil
import subprocess

from bot.utils import is_user_authorized

async def set_bot_menu(application):
    commands = [
        BotCommand("run", "Run a command"),
        BotCommand("get_local_ip", "Get your local IP address"),
        BotCommand("get_public_ip", "Get your public IP address"),
        BotCommand("get_system_info", "Get system information"),
        BotCommand("get_machine_specs", "Get machine specifications"),
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

async def get_machine_specs(update: Update, context) -> None:
    if not is_user_authorized(update.message.from_user.id):
        await update.message.reply_text("❌ You are not authorized to run commands.")
        return

    try:
        cpu_info = platform.processor()
        cpu_count = psutil.cpu_count(logical=True)

        # Get detailed CPU information
        cpu_name = None
        cpu_speed = None
        cpu_vendor = None

        if platform.system() == "Windows":
            cpu_name = cpu_info
        else:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        cpu_name = line.split(":")[1].strip()
                    elif "cpu MHz" in line:
                        cpu_speed = line.split(":")[1].strip()
                    elif "vendor_id" in line:
                        cpu_vendor = line.split(":")[1].strip()

        # Get detailed RAM information
        ram_info = []
        if platform.system() == "Windows":
            result = subprocess.run(["wmic", "MemoryChip", "get", "Manufacturer,Speed,Capacity"], capture_output=True, text=True)
            ram_info.append(result.stdout.strip())
        else:
            ram_details = get_ram_info()
            formatted_ram_info = "\n".join(
                [f"{index + 1}. {ram['Manufacturer']} {ram['Size']} {ram['Type']} {ram['Form Factor']} ({ram['Speed']})" for index, ram in enumerate(ram_details)]
            )
            ram_info.append(formatted_ram_info)

        # Get GPU details
        gpus = GPUtil.getGPUs()
        gpu_name = gpus[0].name if gpus else 'N/A'
        gpu_memory = gpus[0].memoryTotal if gpus else 'N/A'
        gpu_vendor = gpus[0].driver if gpus else 'N/A'

        response = (
            f"CPU: {cpu_name} ({cpu_info})\n"
            f"CPU Cores: {cpu_count}\n"
            f"CPU Speed: {cpu_speed} MHz\n"
            f"CPU Vendor: {cpu_vendor}\n"
            f"RAM Info:\n{ram_info[0]}\n"
            f"GPU: {gpu_name}\n"
            f"GPU Memory: {gpu_memory} MiB\n"
            f"GPU Vendor: {gpu_vendor}\n"
        )
        await update.message.reply_text(response)
    except Exception as e:
        await update.message.reply_text(f'Error retrieving machine specs: {str(e)}')

# Helper function to get detailed RAM information on Linux
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