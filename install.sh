#!/usr/bin/env bash

set -euo pipefail

SERVICE_NAME="telegram_bot.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
BOT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PATH="$BOT_PATH/$SERVICE_NAME"
VENV_PATH="$BOT_PATH/venv"
SERVICE_USER="$(id -un)"
TMP_FILE="$(mktemp "/tmp/${SERVICE_NAME}.XXXXXX")"
trap 'rm -f "$TMP_FILE"' EXIT

if [ "$(id -u)" -eq 0 ]; then
    echo "Run ./install.sh as your normal user, not with sudo."
    echo "The installer will request sudo only when systemd needs it."
    exit 1
fi

mkdir -p "$BOT_PATH/log"

if [ ! -f "$BOT_PATH/.env" ]; then
    cp "$BOT_PATH/.env.sample" "$BOT_PATH/.env"
    chmod 600 "$BOT_PATH/.env"
    echo "Created $BOT_PATH/.env from .env.sample."
    echo "Edit it with your Telegram credentials, then run ./install.sh again."
    exit 1
fi
chmod 600 "$BOT_PATH/.env"

# Check if the service already exists
if [ -f "$SERVICE_PATH" ]; then
    echo "Warning: A service with the name '$SERVICE_NAME' already exists."
    read -r -p "Do you want to overwrite and restart the service? (y/n): " choice
    case "$choice" in
        y|Y ) printf "Proceeding with update.\n";;
        n|N ) echo "Operation aborted."; exit 1;;
        * ) echo "Invalid input. Operation aborted."; exit 1;;
    esac
fi

echo "Creating the Python virtual environment..."
python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/python" -m pip install --upgrade pip
"$VENV_PATH/bin/python" -m pip install -r "$BOT_PATH/requirements.txt"

echo "Validating bot configuration..."
(
    cd "$BOT_PATH"
    "$VENV_PATH/bin/python" -c '
from bot.config import (
    BOT_TOKEN,
    SUPER_ADMIN_USERNAME,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
)
from bot.mtproto import MtprotoDownloader

if not BOT_TOKEN or BOT_TOKEN == "YOUR BOT TOKEN HERE":
    raise SystemExit("BOT_TOKEN is missing or still uses the sample value.")
if not SUPER_ADMIN_USERNAME or SUPER_ADMIN_USERNAME == "your_telegram_username":
    raise SystemExit("SUPER_ADMIN_USERNAME must be set to your Telegram username.")
if bool(TELEGRAM_API_ID) != bool(TELEGRAM_API_HASH):
    raise SystemExit("Set both TELEGRAM_API_ID and TELEGRAM_API_HASH, or neither.")
if TELEGRAM_API_ID and TELEGRAM_API_HASH:
    MtprotoDownloader(
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH,
        bot_token=BOT_TOKEN,
    )
print("Configuration format is valid.")
'
)

# Render the portable service template for this checkout and user.
sed \
    -e "s|__BOT_PATH__|$BOT_PATH|g" \
    -e "s|__BOT_USER__|$SERVICE_USER|g" \
    "$SOURCE_PATH" > "$TMP_FILE"

# Verify if the temp file was created
if [ ! -s "$TMP_FILE" ]; then
    echo "Error: Temporary service file was not created successfully. Check sed commands."
    exit 1
fi

# Move the temp file to the systemd directory with sudo
sudo mv "$TMP_FILE" "$SERVICE_PATH"
trap - EXIT

# Set correct permissions
sudo chmod 644 "$SERVICE_PATH"

# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable "$SERVICE_NAME"

# Restart the service (if it was running)
sudo systemctl restart "$SERVICE_NAME"

# Show service status
sudo systemctl status "$SERVICE_NAME" --no-pager
