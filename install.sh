#!/bin/bash

SERVICE_NAME="telegram_bot.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
SOURCE_PATH="./$SERVICE_NAME"
TMP_FILE="/tmp/$SERVICE_NAME"
BOT_PATH=$(pwd)
USER=$(whoami)

# Check if the service already exists
if [ -f "$SERVICE_PATH" ]; then
    echo "Warning: A service with the name '$SERVICE_NAME' already exists."
    read -p "Do you want to overwrite and restart the service? (y/n): " choice
    case "$choice" in
        y|Y ) printf "Proceeding with update.\nPlease wait 60 sec...";;
        n|N ) echo "Operation aborted."; exit 1;;
        * ) echo "Invalid input. Operation aborted."; exit 1;;
    esac
fi

# Create a temporary service file
sed "s|ExecStart=.*|ExecStart=/usr/bin/python3 $BOT_PATH/main.py >> $BOT_PATH/log/bot_log.txt|" "$SOURCE_PATH" | \
    sed "s|User=.*|User=$USER|" | \
    sed "s|WorkingDirectory=.*|WorkingDirectory=$BOT_PATH|" | \
    sed "s|StandardOutput=.*|StandardOutput=append:$BOT_PATH/log/bot_log.log|" | \
    sed "s|StandardError=.*|StandardError=append:$BOT_PATH/log/bot_log_err.log|" > "$TMP_FILE"

# Verify if the temp file was created
if [ ! -f "$TMP_FILE" ]; then
    echo "Error: Temporary service file was not created successfully. Check sed commands."
    exit 1
fi

# Move the temp file to the systemd directory with sudo
sudo mv "$TMP_FILE" "$SERVICE_PATH"

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

