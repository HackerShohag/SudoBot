# 🤖 SudoBot - Telegram Execution and Remote Assistant Bot

SudoBot is a powerful **Telegram Bot** that allows users to execute commands, retrieve system information, check IP details, and manage system processes—all from Telegram.

## 🚀 Features
- ✅ **Command Execution:** Run shell commands remotely as the configured super admin.
- ⏱️ **Live Command Status:** Follow elapsed time and streamed output in one edited message.
- 🛑 **Chat-wide Stop:** `/stop` interrupts all work running in that chat,
  including shell commands, PDF processing, monitors, and system/IP lookups.
- 🌍 **IP Information:** Fetch local and public IP addresses.
- 🖥️ **System Monitoring:** Get system details and disk usage.
- 📜 **Menu Integration:** Access bot features via a built-in menu.
- 🔑 **Sudo Support:** Securely run commands as superuser.
- 🛠️ **Systemd Service Support:** Run the bot as a background system service.
- 🖨️ **PDF Print Splitting:** Separate uploaded PDFs into B&W and color files.

Long-running work does not block Telegram update handling, so other commands
and `/stop` remain responsive while jobs are in progress. The five-minute
system monitor targets one refreshed reading per second; Telegram rate limits
or network delays can temporarily make an individual update arrive later.

## 🖨️ Splitting PDFs for Printing

You can use the PDF splitter in any of these ways:

1. Reply to a specific PDF with `/splitpdf` or `/splitpdf --duplex`. If it is
   part of a Telegram album, the PDFs in that exact album are processed in
   message order, one at a time.
2. Upload a PDF with `/splitpdf` or `/splitpdf --duplex` as its caption.
3. If prompted for a file, reply directly to that bot prompt with the PDF.

Several separately sent files are not an album and cannot be inferred from one
reply. Non-PDF album members are skipped. A command used as a document caption
processes that document only.

Ordinary document contents are never downloaded unless you explicitly select
them. The bot temporarily remembers only lightweight PDF album metadata so a
later reply can select the complete album.

For one PDF or a PDF album, the bot keeps one status message updated while it
downloads, splits, uploads, and finishes. Album progress moves through each PDF
in order. After the final result PDF, the bot sends one new combined summary
below the outputs with the completed and failed counts; each B&W/color result
still replies to its matching source PDF. Upload statuses show streamed bytes,
percentage, elapsed time, and retry attempts. Files up to 20 MB use
Telegram's hosted Bot API. Larger files automatically use a direct MTProto
download when
`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are configured; no Docker or local
Bot API server is required.

Simplex mode removes color pages from the B&W output and finishes with a short
summary. Duplex mode preserves page positions with blanks and includes the
printing guide showing the sheet side where each color page must be
overprinted. `/printer` is an alias for `/splitpdf`.

In privacy-enabled Telegram groups, the Bot API may not expose files sent before
the command. A standalone PDF can be supplied through the bot's re-upload
prompt. Complete album recovery additionally needs Privacy Mode disabled for
the bot through BotFather, or working `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH` credentials so the bot can retrieve that exact media group
directly.

---

## 🔐 Access Control

Authorized admins can grant or revoke bot access by username or by replying to
the target user's Telegram message:

```text
/authorize @username user
/authorize @username admin
/unauthorize @username
```

When replying to a user, use `/authorize [user|admin]` or `/unauthorize` without
a username. `/remove` remains available as an alias for `/unauthorize`.

---

## 🛠️ Installation

### 1️⃣ Clone the Repository
```bash
git clone https://github.com/yourusername/yourbot.git
cd yourbot
```

### 2️⃣ Setup Virtual Environment (Recommended)
Use Python's built-in virtual environment support for dependency isolation.

#### 📌 Create & Activate Virtual Environment
- **For Linux/macOS:**
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```
- **For Windows:**
  ```powershell
  py -m venv venv
  venv\Scripts\activate
  ```

#### 📌 Deactivate Virtual Environment
```bash
deactivate
```

---

## 📦 Installing Dependencies

#### 📌 Install Required Packages
```bash
python -m pip install -r requirements.txt
```

#### 📌 Update Dependencies
```bash
python -m pip install --upgrade -r requirements.txt
```

#### 📌 Freeze Dependencies
```bash
python -m pip freeze > requirements.txt
```

---

## ⚙️ Configuration

### 📌 Create `.env` File
```bash
cp .env.sample .env
```

### 📌 Edit `.env` File
```ini
BOT_TOKEN=your_telegram_bot_token
TELEGRAM_API_ID=your_numeric_api_id
TELEGRAM_API_HASH=your_api_hash
SUPER_ADMIN_USERNAME=your_telegram_username
```
- Get your `BOT_TOKEN` from [BotFather](https://t.me/BotFather) on Telegram.
- Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from
  [Telegram's API development page](https://my.telegram.org/apps):
  1. Sign in with the phone number attached to your Telegram account.
  2. Open **API development tools** and create an application. The title and
     short name can be anything meaningful to you.
  3. Copy **App api_id** into `TELEGRAM_API_ID` and **App api_hash** into
     `TELEGRAM_API_HASH`.
- Keep the bot token and API hash private. The running bot authenticates its
  own bot account with the bot token; it does not log in as your personal
  Telegram account and does not ask for a phone code at runtime.
- The two Telegram API values are optional for ordinary commands and standalone
  PDFs up to 20 MB. Both are required for the automatic large-file fallback and
  for recovering complete PDF albums after a restart or a metadata cache miss.
- Set `SUPER_ADMIN_USERNAME` to your Telegram username without `@`. This
  explicitly configured account can bootstrap authorization on a fresh
  install and is the only account allowed to run host commands.

---

## 🚀 Running the Bot

### 📌 Start the Bot
```bash
python3 main.py
```

### 📌 Run in the Background (for servers)
```bash
nohup python3 main.py > bot.log 2>&1 &
```

### 📌 Stop the Bot
```bash
pkill -f main.py
```

---

## 🧪 Running Tests

#### 📌 Run All Tests
```bash
python3 -m unittest discover tests
python3 -m unittest discover tests/bot
```

#### 📌 Run Specific Test File
```bash
python3 -m unittest tests.test_menu
```

---

## 🔧 Systemd Service (Auto-Start on Boot)

### 📌 Install the Bot as a systemd Service
Simply run the following script to install the bot as a systemd service:
```bash
./install.sh
```

- The installer creates `venv`, installs `requirements.txt`, renders the
  service for the current checkout/user, and enables it at boot.
- Run the installer as your normal user. It requests `sudo` only for the
  systemd installation steps and validates the `.env` format before restart.
- If `.env` does not exist, the installer creates it from `.env.sample` and
  asks you to fill in the credentials before it installs the service.

---

## 📜 License
This project is licensed under the **GNU General Public License v3.0 (GPLv3)**.  
You can read the full license text in the [LICENSE](LICENSE) file or at [GNU’s official site](https://www.gnu.org/licenses/gpl-3.0.en.html).

---

## 🤝 Contributing
Pull requests are welcome!  
Feel free to fork the repo and submit your changes.

---

## 📞 Contact
- **Telegram**: [Shohag](https://t.me/HackerShohag)
- **GitHub**: [yourusername/yourbot](https://github.com/yourusername/yourbot)

---

🔥 **Developed with ❤️ by Shohag**

---

This version emphasizes that the only step needed for **systemd service** installation is running the `install.sh` script. Let me know if you'd like any further adjustments! 🚀
