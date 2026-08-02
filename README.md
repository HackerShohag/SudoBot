# 🤖 SudoBot - Telegram Execution and Remote Assistant Bot

SudoBot is a powerful **Telegram Bot** that allows users to execute commands, retrieve system information, check IP details, and manage system processes—all from Telegram.

## 🚀 Features
- ✅ **Command Execution:** Run shell commands remotely as the configured super admin.
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

Simplex mode removes color pages from the B&W output. Duplex mode preserves
page positions with blanks and reports the sheet side where each color page
must be overprinted. `/printer` is an alias for `/splitpdf`.

In privacy-enabled Telegram groups, the Bot API may not expose a PDF that was
sent before the command. If that happens, the bot prompts for the file: reply
directly to the bot's prompt with the PDF and it will split it automatically.
Disabling group privacy for the bot through BotFather also lets it receive
ordinary file messages.

---

## 🛠️ Installation

### 1️⃣ Clone the Repository
```bash
git clone https://github.com/yourusername/yourbot.git
cd yourbot
```

### 2️⃣ Setup Virtual Environment (Recommended)
It's recommended to use a **virtual environment** for dependency isolation.

#### 📌 Install `virtualenv`
```bash
pip3 install virtualenv
```

#### 📌 Create & Activate Virtual Environment
- **For Linux/macOS:**
  ```bash
  virtualenv venv
  source venv/bin/activate
  ```
- **For Windows:**
  ```powershell
  virtualenv venv
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
pip3 install -r requirements.txt
```

#### 📌 Update Dependencies
```bash
pip3 install --upgrade -r requirements.txt
```

#### 📌 Freeze Dependencies
```bash
pip3 freeze > requirements.txt
```

---

## ⚙️ Configuration

### 📌 Create `.env` File
```bash
cp .env.example .env
```

### 📌 Edit `.env` File
```ini
BOT_TOKEN=your_telegram_bot_token
SUPER_ADMIN_USERNAME=hackershohag
```
- Get your `BOT_TOKEN` from [BotFather](https://t.me/BotFather) on Telegram.

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
bash install_service.sh
```

- This script will automatically configure and start the bot on boot.

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
