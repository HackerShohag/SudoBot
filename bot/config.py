import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Get values from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
INSTANCE_ROLE = os.getenv("INSTANCE_ROLE", "standalone").strip().casefold()
SERVER_SSH_HOST = os.getenv("SERVER_SSH_HOST", "").strip()
SERVER_SSH_USER = os.getenv("SERVER_SSH_USER", "").strip()
SERVER_SSH_PORT = int(os.getenv("SERVER_SSH_PORT", "22"))
SERVER_SSH_KEY = os.getenv("SERVER_SSH_KEY", "").strip()
SERVER_SSH_KNOWN_HOSTS = os.getenv("SERVER_SSH_KNOWN_HOSTS", "").strip()
HA_HEARTBEAT_FILE = os.getenv(
    "HA_HEARTBEAT_FILE",
    "/tmp/sudobot-primary.heartbeat",
)
HA_TAKEOVER_REQUEST_FILE = os.getenv(
    "HA_TAKEOVER_REQUEST_FILE",
    f"{HA_HEARTBEAT_FILE}.takeover",
)
HA_TAKEOVER_ACK_FILE = os.getenv(
    "HA_TAKEOVER_ACK_FILE",
    f"{HA_HEARTBEAT_FILE}.takeover.ack",
)
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "30"))
FAILOVER_TIMEOUT = float(os.getenv("FAILOVER_TIMEOUT", "90"))
FAILBACK_TIMEOUT = float(os.getenv("FAILBACK_TIMEOUT", "30"))
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
