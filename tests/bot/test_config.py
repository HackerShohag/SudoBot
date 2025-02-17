import unittest
from bot.config import *

class TestConfig(unittest.TestCase):
    def test_example(self):
        self.assertTrue(True)  # Replace with actual tests
        class TestConfig(unittest.TestCase):
            def test_bot_token(self):
                self.assertIsNotNone(BOT_TOKEN, "BOT_TOKEN should not be None")

            def test_max_chars_default(self):
                self.assertEqual(MAX_CHARS, "4096", "MAX_CHARS should default to '4096' if not set")

            def test_max_chars_env(self):
                os.environ["MAX_CHARS"] = "2048"
                load_dotenv()  # Reload environment variables
                self.assertEqual(os.getenv("MAX_CHARS"), "2048", "MAX_CHARS should be '2048' when set in environment variables")
                del os.environ["MAX_CHARS"]  # Clean up environment variable

        if __name__ == "__main__":
            unittest.main()
if __name__ == "__main__":
    unittest.main()
