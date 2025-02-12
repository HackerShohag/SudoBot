from telegram import InlineKeyboardButton, ReplyKeyboardMarkup
import sqlite3

# Initialize the database connection
conn = sqlite3.connect('db/commands_history.db')
cursor = conn.cursor()

# Function to create a user-specific table if it doesn't exist
def create_user_table(user_id):
    cursor.execute(f'''
    CREATE TABLE IF NOT EXISTS command_history_{user_id} (
        command TEXT PRIMARY KEY,
        frequency INTEGER
    )
    ''')
    conn.commit()

# Track the last run command for each user
last_run_command = {}

# Function to generate the inline keyboard with frequent and last commands for a user
def generate_keyboard(user_id):
    global last_run_command

    # Create the user-specific table if it doesn't exist
    create_user_table(user_id)

    # Get the most frequent commands from the user's table
    cursor.execute(f'''
    SELECT command FROM command_history_{user_id}
    ORDER BY frequency DESC
    LIMIT 6
    ''')
    frequent_commands = [row[0] for row in cursor.fetchall()]
    
    # Create buttons for frequent commands and last command
    buttons = []
    
    # Add the most frequent commands to the keyboard in 2 columns
    for i in range(0, len(frequent_commands), 2):
        row = [InlineKeyboardButton(cmd, callback_data=cmd) for cmd in frequent_commands[i:i+2]]
        buttons.append(row)

    # Add the last run command as well
    if user_id in last_run_command and last_run_command[user_id]:
        buttons.append([InlineKeyboardButton(f"{last_run_command[user_id]}", callback_data=last_run_command[user_id])])

    # Return the keyboard layout
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)

async def update_keyboard(update, context):
    user_id = update.message.from_user.id
    # Generate the updated keyboard for the user
    new_keyboard = generate_keyboard(user_id)

    message = await context.bot.send_message(
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
    user_id = update.message.from_user.id
    command = update.message.text  # Get the text of the user's message

    # Create the user-specific table if it doesn't exist
    create_user_table(user_id)

    # Filter commands that start with '/'
    if command.startswith('/'):
        # Update the frequency in the user's table
        cursor.execute(f'''
        INSERT INTO command_history_{user_id} (command, frequency)
        VALUES (?, 1)
        ON CONFLICT(command) DO UPDATE SET frequency = frequency + 1
        ''', (command,))
        conn.commit()

        # Update the last run command for the user
        last_run_command[user_id] = command
