import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from telegram.error import TimedOut

from bot.utils import (
    PDF_UPLOAD_ATTEMPTS,
    PDF_UPLOAD_WRITE_TIMEOUT,
    _duplex_from_args,
    _send_pdf_with_retry,
    _splitpdf_caption_args,
)


class PdfCommandArgumentTests(unittest.TestCase):
    def test_duplex_aliases(self):
        for args in (["-d"], ["--duplex"], ["duplex"]):
            with self.subTest(args=args):
                self.assertTrue(_duplex_from_args(args))

    def test_simplex_is_default(self):
        self.assertFalse(_duplex_from_args([]))
        self.assertFalse(_duplex_from_args(["--simplex"]))

    def test_invalid_arguments_show_usage(self):
        with self.assertRaisesRegex(ValueError, "Usage"):
            _duplex_from_args(["--unknown"])

    def test_pdf_command_can_be_a_document_caption(self):
        self.assertEqual(
            _splitpdf_caption_args("/splitpdf@my_bot --duplex"),
            ["--duplex"],
        )
        self.assertEqual(_splitpdf_caption_args("/printer -d"), ["-d"])
        self.assertIsNone(_splitpdf_caption_args("ordinary caption"))

    def test_upload_timeout_is_long_enough_for_pdf_files(self):
        self.assertGreaterEqual(PDF_UPLOAD_WRITE_TIMEOUT, 300)


class PdfUploadRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_upload_retries_timeouts(self):
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "result.pdf"
            output_path.write_bytes(b"%PDF-1.7 test")
            message = AsyncMock()
            message.reply_document.side_effect = [
                TimedOut("first timeout"),
                TimedOut("second timeout"),
                object(),
            ]

            with patch("bot.utils.asyncio.sleep", new_callable=AsyncMock) as sleep:
                await _send_pdf_with_retry(message, output_path, "B&W pages")

            self.assertEqual(
                message.reply_document.await_count,
                PDF_UPLOAD_ATTEMPTS,
            )
            self.assertEqual(sleep.await_count, PDF_UPLOAD_ATTEMPTS - 1)
            for call in message.reply_document.await_args_list:
                self.assertEqual(
                    call.kwargs["write_timeout"],
                    PDF_UPLOAD_WRITE_TIMEOUT,
                )


if __name__ == "__main__":
    unittest.main()
