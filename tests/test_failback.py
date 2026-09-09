import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main as main_module


NONCE = "0123456789abcdef0123456789abcdef"


class FailbackTests(unittest.IsolatedAsyncioTestCase):
    def test_takeover_request_and_acknowledgment_files(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory, "takeover")
            ack = Path(directory, "takeover.ack")
            request.write_text(f"{NONCE}\n", encoding="ascii")
            with (
                patch.object(
                    main_module,
                    "HA_TAKEOVER_REQUEST_FILE",
                    str(request),
                ),
                patch.object(
                    main_module,
                    "HA_TAKEOVER_ACK_FILE",
                    str(ack),
                ),
            ):
                self.assertEqual(main_module._read_takeover_request(), NONCE)
                main_module._acknowledge_takeover(NONCE)

            self.assertEqual(ack.read_text(encoding="ascii"), f"{NONCE}\n")
            self.assertEqual(ack.stat().st_mode & 0o777, 0o600)

    async def test_active_server_detects_a_new_takeover_request(self):
        with patch.object(
            main_module,
            "_read_takeover_request",
            return_value=NONCE,
        ):
            result = await asyncio.wait_for(
                main_module.wait_for_takeover_request(None),
                timeout=0.1,
            )

        self.assertEqual(result, NONCE)

    async def test_local_waits_for_matching_server_acknowledgment(self):
        ssh = AsyncMock(
            side_effect=[
                (0, b"", b""),
                (0, f"{NONCE}\n".encode(), b""),
            ]
        )
        with (
            patch.object(main_module.secrets, "token_hex", return_value=NONCE),
            patch.object(main_module, "_run_server_ssh", ssh),
        ):
            acknowledged = await main_module.request_server_demotion()

        self.assertTrue(acknowledged)
        self.assertEqual(ssh.await_count, 2)


if __name__ == "__main__":
    unittest.main()
