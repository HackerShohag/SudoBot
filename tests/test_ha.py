import shlex
import unittest
from unittest.mock import patch

from bot import ha


class HighAvailabilityTests(unittest.TestCase):
    def test_server_command_preserves_remote_shell_text_as_one_argument(self):
        command = "printf '%s\\n' \"$(hostname); still data\""
        with patch.multiple(
            ha,
            SERVER_SSH_HOST="server.example",
            SERVER_SSH_USER="botuser",
            SERVER_SSH_PORT=2222,
            SERVER_SSH_KEY="/keys/bot key",
            SERVER_SSH_KNOWN_HOSTS="/keys/known hosts",
        ):
            result = ha.server_command(command)

        arguments = shlex.split(result)
        self.assertEqual(arguments[0], "ssh")
        self.assertIn("botuser@server.example", arguments)
        self.assertEqual(arguments[-1], f"sh -lc {shlex.quote(command)}")

    def test_ssh_configuration_is_required(self):
        with patch.multiple(ha, SERVER_SSH_HOST="", SERVER_SSH_USER=""):
            with self.assertRaisesRegex(RuntimeError, "SERVER_SSH_HOST"):
                ha.server_ssh_args()


if __name__ == "__main__":
    unittest.main()
