# 🤖 SudoBot - Telegram Execution and Remote Assistant Bot

SudoBot is a powerful **Telegram Bot** that allows users to execute commands, retrieve system information, check IP details, and manage system processes—all from Telegram.

## 🚀 Features
- ✅ **Command Execution:** Run shell commands remotely as the configured super admin.
- ⏱️ **Live Command Status:** Follow elapsed time and streamed output in one edited message.
- 🌍 **IP Information:** Fetch local and public IP addresses.
- 🖥️ **System Monitoring:** Get system details and disk usage.
- 📜 **Menu Integration:** Access bot features via a built-in menu.
- 🔑 **Sudo Support:** Securely run commands as superuser.
- 🛠️ **Systemd Service Support:** Run the bot as a background system service.
- 🖨️ **PDF Print Splitting:** Separate uploaded PDFs into B&W and color files.

## 🖨️ Splitting PDFs for Printing

You can use the PDF splitter in any of these ways:

1. Reply to a specific PDF with `/splitpdf` or `/splitpdf --duplex`.
2. Upload a PDF with `/splitpdf` or `/splitpdf --duplex` as its caption.
3. If prompted for a file, reply directly to that bot prompt with the PDF.

Ordinary documents are ignored and are never downloaded by the bot.

The bot keeps one status message updated while it downloads the selected PDF,
splits each page, uploads the B&W and color results, and finishes. Files up to
20 MB use Telegram's hosted Bot API. Larger files automatically use a direct
MTProto download when `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are configured;
no Docker or local Bot API server is required.

Simplex mode removes color pages from the B&W output. Duplex mode preserves
page positions with blanks and reports the sheet side where each color page
must be overprinted. `/printer` is an alias for `/splitpdf`.

In privacy-enabled Telegram groups, the Bot API may not expose a PDF that was
sent before the command. If that happens, the bot prompts for the file: reply
directly to the bot's prompt with the PDF and it will split it automatically.
Disabling group privacy for the bot through BotFather also lets it receive
ordinary file messages.

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
- The two Telegram API values are optional for ordinary commands and PDFs up
  to 20 MB, but both are required for the automatic large-file fallback.
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
