import os

TEST_DIR = "tests"  # Directory to store test files

def ensure_test_directory(path):
    """Ensure the test directory structure exists."""
    os.makedirs(path, exist_ok=True)

def generate_test_file(module_path, relative_path):
    """Generate a unittest file for a given module."""
    test_file_path = os.path.join(TEST_DIR, relative_path, f"test_{os.path.basename(module_path)}.py")
    ensure_test_directory(os.path.dirname(test_file_path))

    if os.path.exists(test_file_path):
        print(f"Test file already exists: {test_file_path}")
        return

    # Convert relative path to module import format
    module_import_path = relative_path.replace("/", ".") + f".{os.path.basename(module_path)}"
    module_import_path = module_import_path.lstrip(".")  # Remove leading dot if needed

    content = f"""import unittest
from {module_import_path} import *

class Test{os.path.basename(module_path).capitalize()}(unittest.TestCase):
    def test_example(self):
        self.assertTrue(True)  # Replace with actual tests

if __name__ == "__main__":
    unittest.main()
"""

    with open(test_file_path, "w") as f:
        f.write(content)

    print(f"Created test file: {test_file_path}")

def scan_directory(base_dir, relative_path=""):
    """Recursively scan the directory and generate test files."""
    for entry in os.scandir(base_dir):
        if entry.is_dir():
            scan_directory(entry.path, os.path.join(relative_path, entry.name))
        elif entry.is_file() and entry.name.endswith(".py") and not entry.name.startswith("test_"):
            generate_test_file(entry.name[:-3], relative_path)

# Ensure tests directory exists
ensure_test_directory(TEST_DIR)

# Scan the current directory and generate test files recursively
scan_directory(os.getcwd())

print("Test file generation complete.")

