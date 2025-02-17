import unittest
from bot.keyboard import *
from telegram import InlineKeyboardButton, ReplyKeyboardMarkup
from bot.keyboard import generate_keyboard, create_user_table, cursor, conn

class TestKeyboard(unittest.TestCase):
    def test_example(self):
        self.assertTrue(True)  # Replace with actual tests
        class TestKeyboard(unittest.TestCase):
            def setUp(self):
                # Set up a test database in memory
                self.conn = sqlite3.connect(':memory:')
                self.cursor = self.conn.cursor()
                self.user_id = 12345
                create_user_table(self.user_id)

            def tearDown(self):
                self.conn.close()

            def test_generate_keyboard_no_commands(self):
                keyboard = generate_keyboard(self.user_id)
                self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
                self.assertEqual(len(keyboard.keyboard), 0)

            def test_generate_keyboard_with_commands(self):
                # Insert test data
                commands = [('/start', 5), ('/help', 3), ('/settings', 2)]
                for command, frequency in commands:
                    self.cursor.execute(f'''
                    INSERT INTO command_history_{self.user_id} (command, frequency)
                    VALUES (?, ?)
                    ''', (command, frequency))
                self.conn.commit()

                keyboard = generate_keyboard(self.user_id)
                self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
                self.assertEqual(len(keyboard.keyboard), 2)
                self.assertEqual(keyboard.keyboard[0][0].text, '/start')
                self.assertEqual(keyboard.keyboard[0][1].text, '/help')

            def test_generate_keyboard_with_last_command(self):
                # Insert test data
                commands = [('/start', 5), ('/help', 3)]
                for command, frequency in commands:
                    self.cursor.execute(f'''
                    INSERT INTO command_history_{self.user_id} (command, frequency)
                    VALUES (?, ?)
                    ''', (command, frequency))
                self.conn.commit()

                # Set the last run command
                last_run_command[self.user_id] = '/settings'

                keyboard = generate_keyboard(self.user_id)
                self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
                self.assertEqual(len(keyboard.keyboard), 2)
                self.assertEqual(keyboard.keyboard[0][0].text, '/start')
                self.assertEqual(keyboard.keyboard[1][0].text, '/settings')

        if __name__ == "__main__":
            unittest.main()
if __name__ == "__main__":
    unittest.main()
