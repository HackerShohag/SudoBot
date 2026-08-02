import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, AsyncMock, patch
from types import SimpleNamespace

import fitz
from telegram.error import BadRequest, TimedOut

from bot.utils import (
    PDF_DOWNLOAD_LIMIT_BYTES,
    PDF_UPLOAD_ATTEMPTS,
    PDF_UPLOAD_WRITE_TIMEOUT,
    PdfDownloadError,
    SplitResult,
    _PdfStatus,
    _download_pdf_document,
    _duplex_from_args,
    _cached_replied_pdf,
    _find_replied_pdf_document,
    _recover_replied_group_pdf,
    _upload_file_path,
    handle_file_upload,
    _send_pdf_with_retry,
    _split_pdf_with_progress,
    _splitpdf_caption_args,
    split_pdf,
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


class RepliedPdfDiscoveryTests(unittest.TestCase):
    @staticmethod
    def document(name="file.pdf", mime_type="application/pdf"):
        return type(
            "Document",
            (),
            {
                "file_name": name,
                "mime_type": mime_type,
                "file_id": "file-id",
            },
        )()

    @staticmethod
    def message(**kwargs):
        defaults = {
            "document": None,
            "effective_attachment": None,
            "external_reply": None,
            "reply_to_message": None,
        }
        defaults.update(kwargs)
        return type("Message", (), defaults)()

    def test_finds_document_in_normal_reply(self):
        document = self.document()
        replied = self.message(document=document)
        command = self.message(reply_to_message=replied)

        self.assertIs(_find_replied_pdf_document(command), document)

    def test_finds_document_in_external_cross_chat_reply(self):
        document = self.document()
        external_reply = type(
            "ExternalReply",
            (),
            {"document": document, "effective_attachment": None},
        )()
        command = self.message(external_reply=external_reply)

        self.assertIs(_find_replied_pdf_document(command), document)

    def test_finds_pdf_in_effective_attachment(self):
        document = self.document()
        replied = self.message(effective_attachment=document)
        command = self.message(reply_to_message=replied)

        self.assertIs(_find_replied_pdf_document(command), document)

    def test_ignores_replied_non_pdf_document(self):
        document = self.document("notes.txt", "text/plain")
        replied = self.message(document=document)
        command = self.message(reply_to_message=replied)

        self.assertIsNone(_find_replied_pdf_document(command))

    def test_upload_paths_are_scoped_by_chat_and_message(self):
        self.assertEqual(
            _upload_file_path(-500, 42, "../../main.pdf"),
            Path("uploads/-500/42/main.pdf"),
        )

    def test_cached_reply_never_uses_another_chat_file(self):
        replied = self.message(
            message_id=42,
            chat=SimpleNamespace(id=-500),
        )
        command = self.message(reply_to_message=replied)
        context = SimpleNamespace(
            chat_data={
                "last_uploaded_pdf": "uploads/100/private.pdf",
                "uploaded_pdfs": {"42": "uploads/100/private.pdf"},
            }
        )

        self.assertIsNone(
            _cached_replied_pdf(command, -500, context)
        )

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


class PdfDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_file_without_mtproto_credentials_is_actionable(self):
        document = SimpleNamespace(
            file_id="large-file",
            file_name="large.pdf",
            file_size=PDF_DOWNLOAD_LIMIT_BYTES + 1,
        )
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock()))

        with self.assertRaisesRegex(PdfDownloadError, "TELEGRAM_API_ID"):
            await _download_pdf_document(context, document, 500, 42)

        context.bot.get_file.assert_not_awaited()

    async def test_known_large_file_uses_mtproto_with_progress(self):
        with TemporaryDirectory() as tmp:
            file_size = 30 * 1024 * 1024
            document = SimpleNamespace(
                file_id="large-file",
                file_name="large.pdf",
                file_size=file_size,
            )
            context = SimpleNamespace(
                bot=SimpleNamespace(get_file=AsyncMock())
            )
            status = SimpleNamespace(update=AsyncMock(return_value=True))
            downloader = SimpleNamespace(download=AsyncMock())

            async def download(file_id, destination, progress):
                self.assertEqual(file_id, "large-file")
                Path(destination).write_bytes(b"%PDF large")
                await progress(file_size // 2, 0)
                return Path(destination)

            downloader.download.side_effect = download
            with (
                patch("bot.utils.UPLOAD_DIR", tmp),
                patch(
                    "bot.utils._get_mtproto_downloader",
                    return_value=downloader,
                ),
            ):
                result = await _download_pdf_document(
                    context,
                    document,
                    500,
                    42,
                    status=status,
                )

            self.assertEqual(result, str(Path(tmp, "500", "42", "large.pdf")))
            self.assertTrue(Path(result).is_file())
            context.bot.get_file.assert_not_awaited()
            downloader.download.assert_awaited_once()
            destination = downloader.download.await_args.args[1]
            self.assertTrue(destination.is_absolute())
            edits = [call.args[0] for call in status.update.await_args_list]
            self.assertTrue(any("Preparing" in text for text in edits))
            self.assertTrue(any("50%" in text for text in edits))

    async def test_small_file_stays_on_hosted_bot_api(self):
        with TemporaryDirectory() as tmp:
            telegram_file = SimpleNamespace(download_to_drive=AsyncMock())
            context = SimpleNamespace(
                bot=SimpleNamespace(
                    get_file=AsyncMock(return_value=telegram_file)
                )
            )
            document = SimpleNamespace(
                file_id="small-file",
                file_name="small.pdf",
                file_size=PDF_DOWNLOAD_LIMIT_BYTES,
            )

            with (
                patch("bot.utils.UPLOAD_DIR", tmp),
                patch("bot.utils._get_mtproto_downloader") as mtproto,
            ):
                result = await _download_pdf_document(
                    context,
                    document,
                    500,
                    42,
                )

            context.bot.get_file.assert_awaited_once_with("small-file")
            telegram_file.download_to_drive.assert_awaited_once_with(result)
            mtproto.assert_not_called()

    async def test_unknown_size_cloud_rejection_retries_with_mtproto(self):
        with TemporaryDirectory() as tmp:
            async def fail_after_partial(path):
                Path(path).write_bytes(b"partial")
                raise BadRequest("File is too big")

            telegram_file = SimpleNamespace(
                download_to_drive=AsyncMock(side_effect=fail_after_partial)
            )
            context = SimpleNamespace(
                bot=SimpleNamespace(
                    get_file=AsyncMock(return_value=telegram_file)
                )
            )
            document = SimpleNamespace(
                file_id="large-file",
                file_name="large.pdf",
                file_size=None,
            )
            downloader = SimpleNamespace(download=AsyncMock())

            async def mtproto_download(file_id, destination, progress):
                self.assertEqual(file_id, "large-file")
                Path(destination).write_bytes(b"%PDF recovered")
                return Path(destination)

            downloader.download.side_effect = mtproto_download

            with (
                patch("bot.utils.UPLOAD_DIR", tmp),
                patch(
                    "bot.utils._get_mtproto_downloader",
                    return_value=downloader,
                ),
            ):
                result = await _download_pdf_document(
                    context,
                    document,
                    500,
                    42,
                )

            self.assertEqual(Path(result).read_bytes(), b"%PDF recovered")
            context.bot.get_file.assert_awaited_once_with("large-file")
            downloader.download.assert_awaited_once()

    async def test_cloud_rejection_without_credentials_removes_partial_file(self):
        with TemporaryDirectory() as tmp:
            async def fail_after_partial(path):
                Path(path).write_bytes(b"partial")
                raise BadRequest("File is too big")

            telegram_file = SimpleNamespace(
                download_to_drive=AsyncMock(side_effect=fail_after_partial)
            )
            context = SimpleNamespace(
                bot=SimpleNamespace(
                    get_file=AsyncMock(return_value=telegram_file)
                )
            )
            document = SimpleNamespace(
                file_id="large-file",
                file_name="large.pdf",
                file_size=None,
            )

            with (
                patch("bot.utils.UPLOAD_DIR", tmp),
                self.assertRaisesRegex(PdfDownloadError, "TELEGRAM_API_HASH"),
            ):
                await _download_pdf_document(context, document, 500, 42)

            self.assertFalse(Path(tmp, "500", "42", "large.pdf").exists())


class PdfStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_forced_status_edit_retries_network_error_without_raising(self):
        message = SimpleNamespace(
            message_id=10,
            edit_text=AsyncMock(
                side_effect=[TimedOut("temporary timeout"), None]
            ),
        )
        status = _PdfStatus(message=message)

        with patch("bot.utils.asyncio.sleep", new_callable=AsyncMock) as sleep:
            updated = await status.update("Still working", force=True)

        self.assertTrue(updated)
        self.assertEqual(message.edit_text.await_count, 2)
        sleep.assert_awaited_once_with(1)

    async def test_status_text_is_limited_to_telegram_message_length(self):
        message = SimpleNamespace(message_id=10, edit_text=AsyncMock())
        status = _PdfStatus(message=message)

        await status.update("x" * 5000, force=True)

        self.assertEqual(len(message.edit_text.await_args.args[0]), 4096)


class PdfProgressWorkflowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def update(message, chat_id=500):
        return SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=chat_id),
        )

    async def test_caption_flow_uses_one_status_for_every_stage(self):
        status_message = SimpleNamespace(
            message_id=700,
            edit_text=AsyncMock(),
        )
        document = SimpleNamespace(
            file_id="pdf-file-id",
            file_name="main.pdf",
            mime_type="application/pdf",
            file_size=654_900,
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=43,
            document=document,
            caption="/splitpdf --duplex",
            reply_to_message=None,
            external_reply=None,
            reply_text=AsyncMock(return_value=status_message),
            reply_document=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(),
            user_data={},
            chat_data={},
        )
        result = SplitResult(
            bw_path=Path("/tmp/main_BW.pdf"),
            color_path=Path("/tmp/main_Color.pdf"),
            bw_pages=3,
            color_pages=1,
            guide="Manual printing guide\n" + ("detail\n" * 1000),
        )

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._download_pdf_document",
                new_callable=AsyncMock,
                return_value="uploads/500/43/main.pdf",
            ),
            patch(
                "bot.utils._split_pdf_with_progress",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch(
                "bot.utils._send_pdf_with_retry",
                new_callable=AsyncMock,
            ) as send_pdf,
        ):
            await handle_file_upload(self.update(message), context)

        message.reply_text.assert_awaited_once()
        self.assertIn(
            "Downloading selected PDF",
            message.reply_text.await_args.args[0],
        )
        edits = [call.args[0] for call in status_message.edit_text.await_args_list]
        self.assertTrue(any("Splitting PDF" in text for text in edits))
        self.assertTrue(any("Uploading B&W pages" in text for text in edits))
        self.assertTrue(any("Uploading Color pages" in text for text in edits))
        self.assertIn("PDF split complete", edits[-1])
        self.assertIn("Mode: Duplex", edits[-1])
        self.assertIn("B&W pages: 3", edits[-1])
        self.assertIn("Color pages: 1", edits[-1])
        self.assertIn("Manual printing guide", edits[-1])
        self.assertLessEqual(len(edits[-1]), 4096)
        self.assertEqual(send_pdf.await_count, 2)

    async def test_oversize_caption_shows_mtproto_setup_in_same_status(self):
        status_message = SimpleNamespace(
            message_id=700,
            edit_text=AsyncMock(),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=43,
            document=SimpleNamespace(
                file_id="large-file-id",
                file_name="large.pdf",
                mime_type="application/pdf",
                file_size=PDF_DOWNLOAD_LIMIT_BYTES + 1,
            ),
            caption="/splitpdf",
            reply_to_message=None,
            reply_text=AsyncMock(return_value=status_message),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_file=AsyncMock()),
            user_data={},
            chat_data={},
        )

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch("bot.utils.split_pdf", new_callable=AsyncMock) as split,
        ):
            await handle_file_upload(self.update(message), context)

        message.reply_text.assert_awaited_once()
        context.bot.get_file.assert_not_awaited()
        split.assert_not_awaited()
        self.assertIn(
            "TELEGRAM_API_ID",
            status_message.edit_text.await_args.args[0],
        )
        self.assertEqual(context.chat_data, {})

    async def test_real_split_worker_reports_without_thread_deadlock(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            document = fitz.open()
            document.new_page()
            document.save(source)
            document.close()
            status_message = SimpleNamespace(
                message_id=700,
                edit_text=AsyncMock(),
            )
            status = _PdfStatus(message=status_message)

            with patch("bot.utils.PDF_STATUS_EDIT_INTERVAL", 0):
                result = await asyncio.wait_for(
                    _split_pdf_with_progress(
                        source,
                        False,
                        root / "out",
                        status,
                    ),
                    timeout=10,
                )

            self.assertEqual(result.bw_pages, 1)
            self.assertTrue(result.bw_path.is_file())


class PendingPdfWorkflowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def update(message, chat_id=500):
        return SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=chat_id),
        )

    async def test_missing_replied_file_starts_pending_duplex_upload(self):
        prompt_message = SimpleNamespace(
            message_id=700,
            edit_text=AsyncMock(),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            reply_to_message=SimpleNamespace(),
            reply_text=AsyncMock(return_value=prompt_message),
        )
        context = SimpleNamespace(
            args=["--duplex"],
            user_data={},
            chat_data={},
        )

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._download_replied_pdf",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "bot.utils._recover_replied_group_pdf",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await split_pdf(self.update(message), context)

        self.assertEqual(
            context.user_data["pending_pdf_split"],
            {
                "args": ["--duplex"],
                "chat_id": 500,
                "prompt_message_id": 700,
            },
        )
        message.reply_text.assert_awaited_once_with(
            "🔎 Locating the selected PDF…"
        )
        prompt = prompt_message.edit_text.await_args.args[0]
        self.assertIn("Reply directly to this bot message", prompt)

    async def test_next_pdf_is_automatically_split_with_pending_args(self):
        telegram_file = SimpleNamespace(download_to_drive=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=43,
            document=SimpleNamespace(
                file_id="pdf-file-id",
                file_name="main.pdf",
                mime_type="application/pdf",
            ),
            caption=None,
            reply_to_message=SimpleNamespace(message_id=700),
            reply_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=telegram_file),
                edit_message_text=AsyncMock(),
            ),
            user_data={
                "pending_pdf_split": {
                    "args": ["--duplex"],
                    "chat_id": 500,
                    "prompt_message_id": 700,
                }
            },
            chat_data={},
        )
        update = self.update(message)

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch("bot.utils.split_pdf", new_callable=AsyncMock) as split,
        ):
            await handle_file_upload(update, context)

        telegram_file.download_to_drive.assert_awaited_once_with(
            "uploads/500/43/main.pdf"
        )
        split.assert_awaited_once_with(
            update,
            context,
            args=["--duplex"],
            input_path="uploads/500/43/main.pdf",
            status=ANY,
        )
        context.bot.edit_message_text.assert_awaited_once()
        self.assertIn(
            "Downloading selected PDF",
            context.bot.edit_message_text.await_args.kwargs["text"],
        )
        self.assertNotIn("pending_pdf_split", context.user_data)

    async def test_explicit_group_reply_never_uses_private_last_upload(self):
        prompt_message = SimpleNamespace(
            message_id=701,
            edit_text=AsyncMock(),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=44,
            reply_to_message=SimpleNamespace(
                message_id=42,
                chat=SimpleNamespace(id=-500),
            ),
            external_reply=None,
            reply_text=AsyncMock(return_value=prompt_message),
        )
        context = SimpleNamespace(
            args=[],
            user_data={"last_uploaded_file": "uploads/private.pdf"},
            chat_data={},
        )

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._download_replied_pdf",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "bot.utils._cached_replied_pdf",
                return_value=None,
            ),
            patch(
                "bot.utils._recover_replied_group_pdf",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await split_pdf(self.update(message, chat_id=-500), context)

        self.assertIn("pending_pdf_split", context.user_data)
        message.reply_text.assert_awaited_once_with(
            "🔎 Locating the selected PDF…"
        )
        prompt = prompt_message.edit_text.await_args.args[0]
        self.assertIn("No downloadable PDF was selected", prompt)

    async def test_ordinary_group_document_is_completely_ignored(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=50,
            document=SimpleNamespace(
                file_id="ordinary-file-id",
                file_name="ordinary.pdf",
                mime_type="application/pdf",
            ),
            caption=None,
            reply_to_message=None,
            reply_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_file=AsyncMock()),
            user_data={},
            chat_data={},
        )

        with patch("bot.utils.is_user_authorized") as authorized:
            await handle_file_upload(self.update(message, chat_id=-500), context)

        context.bot.get_file.assert_not_awaited()
        authorized.assert_not_called()
        message.reply_text.assert_not_awaited()

    async def test_pending_file_not_replying_to_prompt_is_ignored(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=51,
            document=SimpleNamespace(
                file_id="unselected-file-id",
                file_name="unselected.pdf",
                mime_type="application/pdf",
            ),
            caption=None,
            reply_to_message=SimpleNamespace(message_id=999),
            reply_text=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_file=AsyncMock()),
            user_data={
                "pending_pdf_split": {
                    "args": [],
                    "chat_id": -500,
                    "prompt_message_id": 700,
                }
            },
            chat_data={},
        )

        await handle_file_upload(self.update(message, chat_id=-500), context)

        context.bot.get_file.assert_not_awaited()
        self.assertIn("pending_pdf_split", context.user_data)

    async def test_inaccessible_group_pdf_is_recovered_by_private_forward(self):
        document = SimpleNamespace(
            file_id="group-pdf-id",
            file_name="group.pdf",
            mime_type="application/pdf",
        )
        forwarded = SimpleNamespace(
            message_id=99,
            document=document,
            effective_attachment=document,
            external_reply=None,
            reply_to_message=None,
        )
        telegram_file = SimpleNamespace(download_to_drive=AsyncMock())
        bot = SimpleNamespace(
            forward_message=AsyncMock(return_value=forwarded),
            delete_message=AsyncMock(),
            get_file=AsyncMock(return_value=telegram_file),
        )
        message = SimpleNamespace(
            reply_to_message=SimpleNamespace(
                message_id=42,
                chat=SimpleNamespace(id=-500),
            ),
            external_reply=None,
        )
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=-500),
            effective_user=SimpleNamespace(id=100),
        )
        context = SimpleNamespace(bot=bot, chat_data={})

        recovered = await _recover_replied_group_pdf(update, context)

        self.assertEqual(recovered, "uploads/-500/42/group.pdf")
        bot.forward_message.assert_awaited_once_with(
            chat_id=100,
            from_chat_id=-500,
            message_id=42,
            disable_notification=True,
        )
        telegram_file.download_to_drive.assert_awaited_once_with(
            "uploads/-500/42/group.pdf"
        )
        bot.delete_message.assert_awaited_once_with(
            chat_id=100,
            message_id=99,
        )


if __name__ == "__main__":
    unittest.main()
