import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Get values from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_CHARS = os.getenv("MAX_CHARS", "4096")  # Default to "False" if not set