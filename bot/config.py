import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Get values from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_CHARS = os.getenv("MAX_CHARS", "4096")  # Default to "False" if not set
SUPER_ADMIN_USERNAME = os.getenv(
    "SUPER_ADMIN_USERNAME",
    "",
).lstrip("@").casefold()

# Optional MTProto credentials. The normal hosted Bot API continues to work
# without these values; they are only required for downloading files larger
# than Telegram's hosted 20 MB Bot API limit.
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
