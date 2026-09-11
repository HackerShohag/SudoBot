import asyncio
import queue
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import fitz
from telegram import InputFile
from telegram.error import BadRequest, TimedOut

import bot.utils as pdf_utils
from bot import task_registry
from bot.mtproto import MtprotoConfigurationError, MtprotoDocument
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
    _resolve_replied_media_group_sources,
    _split_pdf_result,
    _upload_file_path,
    _split_completion_text,
    handle_file_upload,
    _send_pdf_with_retry,
    _split_pdf_with_progress,
    _splitpdf_caption_args,
    observe_pdf_upload,
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

    def test_printing_guide_is_only_shown_for_duplex(self):
        result = SplitResult(
            bw_path=None,
            color_path=None,
            bw_pages=4,
            color_pages=2,
            guide="Manual printing guide",
        )

        simplex = _split_completion_text(result, False)
        duplex = _split_completion_text(result, True)

        self.assertNotIn("Printing guide", simplex)
        self.assertNotIn("Manual printing guide", simplex)
        self.assertIn("Printing guide", duplex)
        self.assertIn("Manual printing guide", duplex)


class PdfJobConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pdf_utils._active_pdf_jobs.clear()
        task_registry._CHAT_TASKS.clear()

    async def asyncTearDown(self):
        pending = [
            task
            for tasks in task_registry._CHAT_TASKS.values()
            for task in tasks
            if not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        pdf_utils._active_pdf_jobs.clear()
        task_registry._CHAT_TASKS.clear()

    async def test_second_pdf_job_in_same_chat_is_rejected_without_overlap(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_pdf_job(*_args, **_kwargs):
            started.set()
            await release.wait()
            return True

        def objects(message_id):
            message = SimpleNamespace(
                message_id=message_id,
                chat_id=500,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(
                message=message,
                effective_chat=SimpleNamespace(id=500),
            )
            context = SimpleNamespace(args=[], user_data={}, chat_data={})
            return update, context

        first_update, first_context = objects(10)
        second_update, second_context = objects(11)

        with patch(
            "bot.utils._split_pdf_result",
            new=AsyncMock(side_effect=slow_pdf_job),
        ) as split_result:
            first_task = asyncio.create_task(
                split_pdf(first_update, first_context)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            await split_pdf(second_update, second_context)
            release.set()
            await asyncio.wait_for(first_task, timeout=1)

        self.assertEqual(split_result.await_count, 1)
        second_update.message.reply_text.assert_awaited_once_with(
            "⚠️ PDF processing is already running in this chat. "
            "Please wait for it to finish."
        )
        self.assertEqual(pdf_utils._active_pdf_jobs, {})

    async def test_cancelled_pdf_uses_same_status_and_releases_all_tracking(self):
        started = asyncio.Event()
        status_message = SimpleNamespace(
            message_id=700,
            edit_text=AsyncMock(),
        )
        status = _PdfStatus(message=status_message)
        message = SimpleNamespace(
            message_id=10,
            chat_id=500,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=500),
        )
        context = SimpleNamespace(args=[], user_data={}, chat_data={})

        async def blocked_pdf_job(*_args, **_kwargs):
            pdf_utils._set_pdf_job_status(update, status)
            started.set()
            await asyncio.Event().wait()

        with patch(
            "bot.utils._split_pdf_result",
            new=AsyncMock(side_effect=blocked_pdf_job),
        ):
            task = asyncio.create_task(split_pdf(update, context))
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(
                task_registry.tracked_tasks(update),
                {task: "PDF processing"},
            )

            cancelled = task_registry.cancel_chat_tasks(update)
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertEqual(cancelled, [(task, "PDF processing")])
        self.assertTrue(task.cancelled())
        status_message.edit_text.assert_awaited_once_with(
            "🛑 PDF processing stopped."
        )
        message.reply_text.assert_not_awaited()
        self.assertEqual(pdf_utils._active_pdf_jobs, {})
        self.assertEqual(task_registry.tracked_tasks(update), {})

    async def test_cancelled_split_terminates_and_closes_worker_immediately(self):
        class Worker:
            def __init__(self):
                self.alive = True
                self.started = False
                self.terminate = Mock(side_effect=self._terminate)
                self.kill = Mock(side_effect=self._kill)
                self.close = Mock()

            def start(self):
                self.started = True

            def is_alive(self):
                return self.alive

            def _terminate(self):
                self.alive = False

            def _kill(self):
                self.alive = False

        worker = Worker()
        events = SimpleNamespace(
            get_nowait=Mock(side_effect=queue.Empty),
            close=Mock(),
        )
        process_context = SimpleNamespace(
            Queue=Mock(return_value=events),
            Process=Mock(return_value=worker),
        )
        status = SimpleNamespace(update=AsyncMock())

        with patch(
            "bot.utils.multiprocessing.get_context",
            return_value=process_context,
        ):
            task = asyncio.create_task(
                _split_pdf_with_progress("source.pdf", False, "out", status)
            )
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(task.cancelled())
        self.assertTrue(worker.started)
        worker.terminate.assert_called_once_with()
        worker.kill.assert_not_called()
        worker.close.assert_called_once_with()
        events.close.assert_called_once_with()


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
    async def test_pdf_upload_streams_real_byte_progress_to_one_status(self):
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "result.pdf"
            output_path.write_bytes(b"0123456789")
            halfway_reported = asyncio.Event()
            status_texts = []
            streamed_documents = []

            async def record_status(text, force=False):
                status_texts.append(text)
                if "(50%)" in text:
                    halfway_reported.set()
                return True

            async def upload_document(**kwargs):
                document = kwargs["document"]
                self.assertIsInstance(document, InputFile)
                self.assertEqual(kwargs["reply_to_message_id"], 42)
                self.assertTrue(kwargs["allow_sending_without_reply"])
                self.assertEqual(kwargs["caption"], "main.pdf — B&W pages")
                streamed_documents.append(document)
                tracked_file = document.input_file_content
                self.assertNotIsInstance(tracked_file, bytes)
                tracked_file.seek(0)
                self.assertEqual(tracked_file.read(5), b"01234")
                await asyncio.wait_for(halfway_reported.wait(), timeout=1)
                self.assertEqual(tracked_file.read(), b"56789")

            message = SimpleNamespace(
                reply_document=AsyncMock(side_effect=upload_document)
            )
            status = SimpleNamespace(
                update=AsyncMock(side_effect=record_status)
            )

            with patch("bot.utils.PDF_UPLOAD_STATUS_INTERVAL", 0.001):
                await _send_pdf_with_retry(
                    message,
                    output_path,
                    "B&W pages",
                    status=status,
                    reply_to_message_id=42,
                    caption="main.pdf — B&W pages",
                )

            self.assertEqual(len(streamed_documents), 1)
            self.assertEqual(streamed_documents[0].filename, "result.pdf")
            self.assertTrue(any("(0%)" in text for text in status_texts))
            self.assertTrue(any("(50%)" in text for text in status_texts))
            self.assertTrue(any("(100%)" in text for text in status_texts))
            self.assertTrue(any("Elapsed:" in text for text in status_texts))
            self.assertIn("Uploaded B&W pages", status_texts[-1])

    async def test_pdf_upload_retry_resets_progress_and_shows_attempt(self):
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "result.pdf"
            output_path.write_bytes(b"retry-data")
            attempts = 0
            status_texts = []

            async def upload_document(**kwargs):
                nonlocal attempts
                attempts += 1
                tracked_file = kwargs["document"].input_file_content
                tracked_file.seek(0)
                if attempts == 1:
                    tracked_file.read(5)
                    raise TimedOut("temporary upload failure")
                tracked_file.read()

            async def record_status(text, force=False):
                status_texts.append(text)
                return True

            message = SimpleNamespace(
                reply_document=AsyncMock(side_effect=upload_document)
            )
            status = SimpleNamespace(
                update=AsyncMock(side_effect=record_status)
            )

            with patch(
                "bot.utils.asyncio.sleep",
                new_callable=AsyncMock,
            ):
                await _send_pdf_with_retry(
                    message,
                    output_path,
                    "Color pages",
                    status=status,
                )

            self.assertEqual(attempts, 2)
            self.assertTrue(
                any("Retrying attempt 2/3" in text for text in status_texts)
            )
            second_attempt = next(
                text for text in status_texts if "Attempt: 2/3" in text
            )
            self.assertIn("(0%)", second_attempt)
            self.assertIn("(100%)", status_texts[-1])

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

        with (
            patch("bot.utils.TELEGRAM_API_ID", None),
            patch("bot.utils.TELEGRAM_API_HASH", None),
            patch("bot.utils._mtproto_downloader", None),
            self.assertRaisesRegex(PdfDownloadError, "TELEGRAM_API_ID"),
        ):
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

    async def test_recovered_album_document_keeps_the_mtproto_transport(self):
        with TemporaryDirectory() as tmp:
            context = SimpleNamespace(
                bot=SimpleNamespace(get_file=AsyncMock())
            )
            document = MtprotoDocument(
                message_id=42,
                media_group_id="album-one",
                file_id="direct-file",
                file_unique_id="direct-unique",
                file_name="small.pdf",
                mime_type="application/pdf",
                file_size=1_024,
            )
            downloader = SimpleNamespace(download=AsyncMock())

            async def download(_file_id, destination, _progress):
                Path(destination).write_bytes(b"%PDF direct")
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
                )

            self.assertEqual(Path(result).read_bytes(), b"%PDF direct")
            context.bot.get_file.assert_not_awaited()
            downloader.download.assert_awaited_once()

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
                patch("bot.utils.TELEGRAM_API_ID", None),
                patch("bot.utils.TELEGRAM_API_HASH", None),
                patch("bot.utils._mtproto_downloader", None),
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

        async def fake_upload(_message, _path, label, *, status, **_kwargs):
            await status.update(
                f"⬆️ Uploading {label}: 50%…",
                force=True,
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
            send_pdf.side_effect = fake_upload
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
        first_status = send_pdf.await_args_list[0].kwargs["status"]
        second_status = send_pdf.await_args_list[1].kwargs["status"]
        self.assertIs(first_status, second_status)

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
            patch("bot.utils.TELEGRAM_API_ID", None),
            patch("bot.utils.TELEGRAM_API_HASH", None),
            patch("bot.utils._mtproto_downloader", None),
            patch(
                "bot.utils._split_pdf_result",
                new_callable=AsyncMock,
            ) as split,
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


class PdfAlbumWorkflowTests(unittest.IsolatedAsyncioTestCase):
    chat_id = -500

    @staticmethod
    def document(message_id, *, name=None, mime_type="application/pdf"):
        return SimpleNamespace(
            file_id=f"file-{message_id}",
            file_unique_id=f"unique-{message_id}",
            file_name=name or f"source-{message_id}.pdf",
            mime_type=mime_type,
            file_size=1_024,
        )

    @classmethod
    def album_message(
        cls,
        message_id,
        *,
        media_group_id="album-one",
        name=None,
        mime_type="application/pdf",
    ):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=message_id,
            media_group_id=media_group_id,
            document=cls.document(
                message_id,
                name=name,
                mime_type=mime_type,
            ),
            caption=None,
            reply_to_message=None,
            external_reply=None,
            chat=SimpleNamespace(id=cls.chat_id),
            reply_text=AsyncMock(),
        )

    @classmethod
    def update(cls, message):
        return SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=SimpleNamespace(id=cls.chat_id),
        )

    @staticmethod
    def status_message(message_id):
        return SimpleNamespace(
            message_id=message_id,
            edit_text=AsyncMock(),
        )

    @classmethod
    def command_message(cls, replied, status_messages):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=100,
            document=None,
            caption=None,
            media_group_id=None,
            reply_to_message=replied,
            external_reply=None,
            chat=SimpleNamespace(id=cls.chat_id),
            reply_text=AsyncMock(side_effect=status_messages),
            reply_document=AsyncMock(),
        )

    @staticmethod
    def context():
        return SimpleNamespace(
            args=[],
            bot=SimpleNamespace(get_file=AsyncMock()),
            bot_data={},
            chat_data={},
            user_data={},
        )

    async def remember(self, context, *messages):
        for message in messages:
            await observe_pdf_upload(self.update(message), context)

    async def test_observer_caches_only_album_metadata_without_downloading(self):
        context = self.context()
        selected = [
            self.album_message(12),
            self.album_message(10),
            self.album_message(11),
        ]
        unrelated = self.album_message(20, media_group_id="album-two")
        non_pdf = self.album_message(
            13,
            name="notes.txt",
            mime_type="text/plain",
        )

        with patch("bot.utils.is_user_authorized") as authorized:
            await self.remember(
                context,
                *selected,
                unrelated,
                non_pdf,
            )

        cache = context.bot_data["_pdf_media_groups"]
        self.assertEqual(
            sorted(cache[(self.chat_id, "album-one")]["items"]),
            [10, 11, 12],
        )
        self.assertEqual(
            sorted(cache[(self.chat_id, "album-two")]["items"]),
            [20],
        )
        context.bot.get_file.assert_not_awaited()
        authorized.assert_not_called()
        for message in (*selected, unrelated, non_pdf):
            message.reply_text.assert_not_awaited()

    async def test_album_metadata_cache_is_strictly_bounded(self):
        context = self.context()

        with patch("bot.utils.PDF_MEDIA_GROUP_CACHE_LIMIT", 2):
            await self.remember(
                context,
                self.album_message(10, media_group_id="album-one"),
                self.album_message(20, media_group_id="album-two"),
                self.album_message(30, media_group_id="album-three"),
            )

        cache = context.bot_data["_pdf_media_groups"]
        self.assertEqual(len(cache), 2)
        self.assertNotIn((self.chat_id, "album-one"), cache)

    async def test_cache_miss_recovers_exact_album_through_mtproto(self):
        replied = self.album_message(22)
        command = self.command_message(replied, [])
        context = self.context()
        recovered = [
            MtprotoDocument(
                message_id=23,
                media_group_id="album-one",
                file_id="file-23",
                file_unique_id="unique-23",
                file_name="third.pdf",
                mime_type="application/pdf",
                file_size=300,
            ),
            MtprotoDocument(
                message_id=22,
                media_group_id="album-one",
                file_id="file-22",
                file_unique_id="unique-22",
                file_name="second.pdf",
                mime_type="application/pdf",
                file_size=200,
            ),
        ]
        downloader = SimpleNamespace(
            get_media_group_documents=AsyncMock(return_value=recovered)
        )

        with patch(
            "bot.utils._get_mtproto_downloader",
            return_value=downloader,
        ):
            selection = await _resolve_replied_media_group_sources(
                self.update(command),
                context,
            )

        downloader.get_media_group_documents.assert_awaited_once_with(
            self.chat_id,
            22,
        )
        self.assertTrue(selection.verified_complete)
        self.assertEqual(
            [source.message_id for source in selection.sources],
            [22, 23],
        )
        self.assertTrue(
            all(source.document.mtproto_only for source in selection.sources)
        )

    async def test_cached_album_is_marked_unverified_when_direct_lookup_is_offline(self):
        context = self.context()
        sources = [self.album_message(10), self.album_message(11)]
        await self.remember(context, *sources)
        command = self.command_message(sources[0], [])

        with patch(
            "bot.utils._get_mtproto_downloader",
            side_effect=MtprotoConfigurationError("credentials unavailable"),
        ):
            selection = await _resolve_replied_media_group_sources(
                self.update(command),
                context,
            )

        self.assertFalse(selection.verified_complete)
        self.assertEqual(
            [source.message_id for source in selection.sources],
            [10, 11],
        )

    async def test_replied_album_processes_pdfs_in_order_with_one_status(self):
        context = self.context()
        sources = [
            self.album_message(12, name="third.pdf"),
            self.album_message(10, name="first.pdf"),
            self.album_message(11, name="second.pdf"),
        ]
        await self.remember(context, *sources)
        batch_status = self.status_message(700)
        final_message = self.status_message(701)
        timeline = []
        reply_calls = 0

        async def reply_text(text, **_kwargs):
            nonlocal reply_calls
            reply_calls += 1
            timeline.append(("reply", text))
            return batch_status if reply_calls == 1 else final_message

        command = self.command_message(sources[2], reply_text)
        update = self.update(command)
        downloader = SimpleNamespace(
            get_media_group_documents=AsyncMock(return_value=[])
        )
        split_result = SplitResult(
            bw_path=Path("/tmp/result_BW.pdf"),
            color_path=Path("/tmp/result_Color.pdf"),
            bw_pages=2,
            color_pages=1,
            guide="",
        )

        async def download(_context, document, chat_id, message_id, **_kwargs):
            return f"uploads/{chat_id}/{message_id}/{document.file_name}"

        async def send_output(_message, _path, _label, **kwargs):
            timeline.append(("send", kwargs["caption"]))

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._get_mtproto_downloader",
                return_value=downloader,
            ),
            patch(
                "bot.utils._download_pdf_document",
                new_callable=AsyncMock,
                side_effect=download,
            ) as download_pdf,
            patch(
                "bot.utils._split_pdf_with_progress",
                new_callable=AsyncMock,
                return_value=split_result,
            ) as split_progress,
            patch(
                "bot.utils._send_pdf_with_retry",
                new_callable=AsyncMock,
                side_effect=send_output,
            ) as send_pdf,
        ):
            completed = await split_pdf(update, context)

        # Telegram handler callbacks must return None so ConversationHandler
        # does not interpret bool as an unknown conversation state.
        self.assertIsNone(completed)
        downloader.get_media_group_documents.assert_awaited_once_with(
            self.chat_id,
            11,
        )
        self.assertEqual(
            [call.args[3] for call in download_pdf.await_args_list],
            [10, 11, 12],
        )
        self.assertEqual(split_progress.await_count, 3)
        self.assertEqual(send_pdf.await_count, 6)
        self.assertEqual(
            [
                call.kwargs["reply_to_message_id"]
                for call in send_pdf.await_args_list
            ],
            [10, 10, 11, 11, 12, 12],
        )
        self.assertEqual(
            [call.kwargs["caption"] for call in send_pdf.await_args_list],
            [
                "first.pdf — B&W pages",
                "first.pdf — Color pages",
                "second.pdf — B&W pages",
                "second.pdf — Color pages",
                "third.pdf — B&W pages",
                "third.pdf — Color pages",
            ],
        )
        self.assertEqual(command.reply_text.await_count, 2)
        first_reply = command.reply_text.await_args_list[0]
        self.assertEqual(
            first_reply.args[0],
            pdf_utils.animated_status_text(
                "Locating PDFs in the selected album…"
            ),
        )
        self.assertEqual(first_reply.kwargs["reply_to_message_id"], 11)
        self.assertTrue(first_reply.kwargs["allow_sending_without_reply"])
        final_summary = "✅ All PDFs have been processed: 3/3 completed."
        second_reply = command.reply_text.await_args_list[1]
        self.assertEqual(second_reply.args[0], final_summary)
        self.assertEqual(second_reply.kwargs["reply_to_message_id"], 100)
        self.assertTrue(second_reply.kwargs["allow_sending_without_reply"])
        self.assertEqual(timeline[-1], ("reply", final_summary))
        self.assertEqual(
            [event[0] for event in timeline].count("send"),
            6,
        )
        final_message.edit_text.assert_not_awaited()
        shared_status = download_pdf.await_args_list[0].kwargs["status"]
        self.assertTrue(
            all(
                call.kwargs["status"] is shared_status
                for call in download_pdf.await_args_list
            )
        )
        self.assertTrue(
            all(
                call.args[3] is shared_status
                for call in split_progress.await_args_list
            )
        )
        self.assertTrue(
            all(
                call.kwargs["status"] is shared_status
                for call in send_pdf.await_args_list
            )
        )

        edits = [
            call.args[0]
            for call in batch_status.edit_text.await_args_list
        ]
        prefixes = [
            "📄 PDFs: 1/3 — first.pdf",
            "📄 PDFs: 2/3 — second.pdf",
            "📄 PDFs: 3/3 — third.pdf",
        ]
        prefix_positions = [
            next(
                index
                for index, text in enumerate(edits)
                if text.startswith(prefix)
            )
            for prefix in prefixes
        ]
        self.assertEqual(prefix_positions, sorted(prefix_positions))
        self.assertFalse(any("every PDF" in text for text in edits))
        self.assertFalse(any("PDF split complete" in text for text in edits))
        self.assertEqual(
            edits[-1],
            "✅ Finished processing PDFs.",
        )

    async def test_one_album_failure_does_not_stop_later_pdfs(self):
        context = self.context()
        sources = [self.album_message(10), self.album_message(11)]
        await self.remember(context, *sources)
        batch_status = self.status_message(710)
        command = self.command_message(sources[0], [batch_status, None])
        downloader = SimpleNamespace(
            get_media_group_documents=AsyncMock(return_value=[])
        )
        split_result = SplitResult(
            bw_path=Path("/tmp/result_BW.pdf"),
            color_path=None,
            bw_pages=1,
            color_pages=0,
            guide="",
        )

        async def download(_context, document, chat_id, message_id, **_kwargs):
            if message_id == 10:
                raise PdfDownloadError("❌ first download failed")
            return f"uploads/{chat_id}/{message_id}/{document.file_name}"

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._get_mtproto_downloader",
                return_value=downloader,
            ),
            patch(
                "bot.utils._download_pdf_document",
                new_callable=AsyncMock,
                side_effect=download,
            ) as download_pdf,
            patch(
                "bot.utils._split_pdf_with_progress",
                new_callable=AsyncMock,
                return_value=split_result,
            ) as split_progress,
            patch(
                "bot.utils._send_pdf_with_retry",
                new_callable=AsyncMock,
            ) as send_pdf,
        ):
            completed = await _split_pdf_result(
                self.update(command),
                context,
            )

        self.assertFalse(completed)
        self.assertEqual(
            [call.args[3] for call in download_pdf.await_args_list],
            [10, 11],
        )
        split_progress.assert_awaited_once()
        send_pdf.assert_awaited_once()
        self.assertEqual(
            send_pdf.await_args.kwargs["reply_to_message_id"],
            11,
        )
        self.assertEqual(command.reply_text.await_count, 2)
        final_summary = command.reply_text.await_args_list[1]
        self.assertEqual(
            final_summary.args[0],
            "⚠️ Album processing finished: 1/2 PDFs completed, 1 failed.",
        )
        self.assertEqual(final_summary.kwargs["reply_to_message_id"], 100)
        self.assertTrue(
            final_summary.kwargs["allow_sending_without_reply"]
        )
        edits = [
            call.args[0]
            for call in batch_status.edit_text.await_args_list
        ]
        self.assertTrue(
            any(
                text.startswith("📄 PDFs: 1/2 — source-10.pdf")
                and "first download failed" in text
                for text in edits
            )
        )
        self.assertTrue(
            any(
                text.startswith("📄 PDFs: 2/2 — source-11.pdf")
                for text in edits
            )
        )
        self.assertEqual(
            edits[-1],
            "⚠️ Finished processing PDFs with failures.",
        )

    async def test_shared_status_edit_timeout_does_not_abort_the_album(self):
        context = self.context()
        sources = [self.album_message(10), self.album_message(11)]
        await self.remember(context, *sources)
        status_edits = []

        async def edit_status(text):
            status_edits.append(text)
            if len(status_edits) == 1:
                raise TimedOut("status edit timed out")

        batch_status = SimpleNamespace(
            message_id=720,
            edit_text=AsyncMock(side_effect=edit_status),
        )
        command = self.command_message(sources[0], [batch_status, None])
        downloader = SimpleNamespace(
            get_media_group_documents=AsyncMock(return_value=[])
        )
        split_result = SplitResult(
            bw_path=Path("/tmp/result_BW.pdf"),
            color_path=None,
            bw_pages=1,
            color_pages=0,
            guide="",
        )

        async def download(_context, document, chat_id, message_id, **_kwargs):
            return f"uploads/{chat_id}/{message_id}/{document.file_name}"

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._get_mtproto_downloader",
                return_value=downloader,
            ),
            patch(
                "bot.utils._download_pdf_document",
                new_callable=AsyncMock,
                side_effect=download,
            ) as download_pdf,
            patch(
                "bot.utils._split_pdf_with_progress",
                new_callable=AsyncMock,
                return_value=split_result,
            ),
            patch(
                "bot.utils._send_pdf_with_retry",
                new_callable=AsyncMock,
            ),
            patch(
                "bot.utils.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            completed = await _split_pdf_result(
                self.update(command),
                context,
            )

        self.assertTrue(completed)
        self.assertEqual(download_pdf.await_count, 2)
        self.assertEqual(command.reply_text.await_count, 2)
        sleep.assert_awaited_once_with(1)
        self.assertEqual(
            command.reply_text.await_args_list[1].args[0],
            "✅ All PDFs have been processed: 2/2 completed.",
        )
        self.assertEqual(
            status_edits[-1],
            "✅ Finished processing PDFs.",
        )

    async def test_final_summary_send_failure_falls_back_to_shared_status(self):
        context = self.context()
        sources = [self.album_message(10), self.album_message(11)]
        await self.remember(context, *sources)
        batch_status = self.status_message(730)
        command = self.command_message(
            sources[0],
            [batch_status, TimedOut("summary send timed out")],
        )
        downloader = SimpleNamespace(
            get_media_group_documents=AsyncMock(return_value=[])
        )
        split_result = SplitResult(
            bw_path=Path("/tmp/result_BW.pdf"),
            color_path=None,
            bw_pages=1,
            color_pages=0,
            guide="",
        )

        async def download(_context, document, chat_id, message_id, **_kwargs):
            return f"uploads/{chat_id}/{message_id}/{document.file_name}"

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._get_mtproto_downloader",
                return_value=downloader,
            ),
            patch(
                "bot.utils._download_pdf_document",
                new_callable=AsyncMock,
                side_effect=download,
            ),
            patch(
                "bot.utils._split_pdf_with_progress",
                new_callable=AsyncMock,
                return_value=split_result,
            ),
            patch(
                "bot.utils._send_pdf_with_retry",
                new_callable=AsyncMock,
            ),
            patch("bot.utils.logger.warning") as warning,
        ):
            completed = await _split_pdf_result(
                self.update(command),
                context,
            )

        self.assertTrue(completed)
        self.assertEqual(command.reply_text.await_count, 2)
        warning.assert_called_once_with(
            "Could not send the final PDF album summary",
            exc_info=True,
        )
        self.assertEqual(
            batch_status.edit_text.await_args.args[0],
            "✅ All PDFs have been processed: 2/2 completed.",
        )


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
            handler_result = await split_pdf(self.update(message), context)

        self.assertIsNone(handler_result)
        self.assertEqual(
            context.chat_data["pending_pdf_split"],
            {
                "args": ["--duplex"],
                "chat_id": 500,
                "prompt_message_id": 700,
            },
        )
        message.reply_text.assert_awaited_once_with(
            pdf_utils.animated_status_text("Locating the selected PDF…")
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
            user_data={},
            chat_data={
                "pending_pdf_split": {
                    "args": ["--duplex"],
                    "chat_id": 500,
                    "prompt_message_id": 700,
                }
            },
        )
        update = self.update(message)

        with (
            patch("bot.utils.is_user_authorized", return_value=True),
            patch(
                "bot.utils._split_pdf_result",
                new_callable=AsyncMock,
            ) as split,
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
        self.assertNotIn("pending_pdf_split", context.chat_data)

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

        self.assertIn("pending_pdf_split", context.chat_data)
        message.reply_text.assert_awaited_once_with(
            pdf_utils.animated_status_text("Locating the selected PDF…")
        )
        prompt = prompt_message.edit_text.await_args.args[0]
        self.assertIn("No downloadable PDF was selected", prompt)

    async def test_ordinary_group_document_is_completely_ignored(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=100, username="user"),
            message_id=50,
            media_group_id="album-one",
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
            bot_data={},
            user_data={},
            chat_data={},
        )

        with patch("bot.utils.is_user_authorized") as authorized:
            await handle_file_upload(self.update(message, chat_id=-500), context)

        context.bot.get_file.assert_not_awaited()
        authorized.assert_not_called()
        message.reply_text.assert_not_awaited()
        cached = context.bot_data["_pdf_media_groups"][
            (-500, "album-one")
        ]["items"]
        self.assertEqual(list(cached), [50])

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
            user_data={},
            chat_data={
                "pending_pdf_split": {
                    "args": [],
                    "chat_id": -500,
                    "prompt_message_id": 700,
                }
            },
        )

        await handle_file_upload(self.update(message, chat_id=-500), context)

        context.bot.get_file.assert_not_awaited()
        self.assertIn("pending_pdf_split", context.chat_data)

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
