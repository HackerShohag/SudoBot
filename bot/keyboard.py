from telegram import InlineKeyboardButton, ReplyKeyboardMarkup
import sqlite3

# Initialize the database connection
conn = sqlite3.connect('db/commands_history.db')
cursor = conn.cursor()

# Create the table to store command frequencies if it doesn't exist
cursor.execute('''
CREATE TABLE IF NOT EXISTS command_history (
    command TEXT PRIMARY KEY,
    frequency INTEGER
)
''')
conn.commit()

# Track the last run command
last_run_command = ""

# Function to generate the inline keyboard with frequent and last commands
def generate_keyboard():
    global last_run_command

    # Get the most frequent commands from the database
    cursor.execute('''
    SELECT command FROM command_history
    ORDER BY frequency DESC
    LIMIT 4
    ''')
    frequent_commands = [row[0] for row in cursor.fetchall()]
    
    # Create buttons for frequent commands and last command
    buttons = []
    
    # Add the most frequent commands to the keyboard in 2 columns
    for i in range(0, len(frequent_commands), 2):
        row = [InlineKeyboardButton(cmd, callback_data=cmd) for cmd in frequent_commands[i:i+2]]
        buttons.append(row)

    # Add the last run command as well
    if last_run_command:
        buttons.append([InlineKeyboardButton(f"{last_run_command}", callback_data=last_run_command)])

    # Return the keyboard layout
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)

async def update_keyboard(update, context):
    # Generate the updated keyboard (you can use your existing `generate_keyboard()` function)
    new_keyboard = generate_keyboard()

    await context.bot.send_message(
        chat_id=update.message.chat_id,
        text="Frequently used commands:",
        reply_markup=new_keyboard
    )

# Function to handle command re-execution from the inline keyboard
async def handle_command_from_keyboard(update, context, execute_command_callback):
    query = update.callback_query
    command = query.data  # Get the command from the callback data
    
    # Execute the command and reply to the original message
    await execute_command_callback(command, update, context)
    
    # Acknowledge the callback query to remove the "loading" state on the button
    await query.answer()

def update_command_history(update, context):
    global last_run_command
    command = update.message.text  # Get the text of the user's message

    # Filter commands that start with '/'
    if command.startswith('/'):
        # Update the frequency in the database
        cursor.execute('''
        INSERT INTO command_history (command, frequency)
        VALUES (?, 1)
        ON CONFLICT(command) DO UPDATE SET frequency = frequency + 1
        ''', (command,))
        conn.commit()

        # Update the last run command
        last_run_command = command
