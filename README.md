# 🤖 SudoBot - Telegram Execution and Remote Assistant Bot

SudoBot is a powerful **Telegram Bot** that allows users to execute commands, retrieve system information, check IP details, and manage system processes—all from Telegram.

## 🚀 Features
- ✅ **Command Execution:** Run shell commands remotely.
- 🌍 **IP Information:** Fetch local and public IP addresses.
- 🖥️ **System Monitoring:** Get system details and disk usage.
- 📜 **Menu Integration:** Access bot features via a built-in menu.
- 🔑 **Sudo Support:** Securely run commands as superuser.
- 🛠️ **Systemd Service Support:** Run the bot as a background system service.

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
