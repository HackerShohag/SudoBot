import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from bot.mtproto import (
    MtprotoConfigurationError,
    MtprotoConnectionError,
    MtprotoDependencyError,
    MtprotoDownloadError,
    MtprotoDownloader,
    MtprotoMediaGroupError,
    MtprotoPathError,
)


class FakeClient:
    instances = []
    active_downloads = 0
    maximum_active_downloads = 0
    media_group_calls = []
    media_group_messages = []
    media_group_error = None

    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.instances.append(self)

    async def start(self):
        self.started += 1
        return self

    async def stop(self):
        self.stopped += 1

    async def download_media(self, file_id, *, file_name, progress):
        type(self).active_downloads += 1
        type(self).maximum_active_downloads = max(
            type(self).maximum_active_downloads,
            type(self).active_downloads,
        )
        try:
            await asyncio.sleep(0)
            if progress is not None:
                await progress(4, 10)
            Path(file_name).write_bytes(file_id.encode())
            await asyncio.sleep(0)
            return file_name
        finally:
            type(self).active_downloads -= 1

    async def get_media_group(self, chat_id, message_id):
        type(self).media_group_calls.append((chat_id, message_id))
        if type(self).media_group_error is not None:
            raise type(self).media_group_error
        return type(self).media_group_messages


class MtprotoConfigurationTests(unittest.TestCase):
    def test_credentials_are_validated_without_importing_kurigram(self):
        with self.assertRaises(MtprotoConfigurationError):
            MtprotoDownloader(api_id="", api_hash="hash", bot_token="token")
        with self.assertRaises(MtprotoConfigurationError):
            MtprotoDownloader(api_id=1, api_hash="", bot_token="token")
        with self.assertRaises(MtprotoConfigurationError):
            MtprotoDownloader(api_id=1, api_hash="a" * 32, bot_token="")
        with self.assertRaisesRegex(
            MtprotoConfigurationError,
            "32-character hexadecimal",
        ):
            MtprotoDownloader(
                api_id=1,
                api_hash="not-an-api-hash",
                bot_token="token",
            )


class MtprotoDownloaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeClient.instances.clear()
        FakeClient.active_downloads = 0
        FakeClient.maximum_active_downloads = 0
        FakeClient.media_group_calls.clear()
        FakeClient.media_group_messages = []
        FakeClient.media_group_error = None
        self.downloader = MtprotoDownloader(
            api_id="12345",
            api_hash="a" * 32,
            bot_token="123:bot-secret",
            client_factory=FakeClient,
        )

    async def asyncTearDown(self):
        await self.downloader.close()

    async def test_uses_an_in_memory_bot_session_and_reports_progress(self):
        progress_updates = []

        async def progress(current, total):
            progress_updates.append((current, total))

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "nested" / "large.pdf"
            result = await self.downloader.download(
                "telegram-file-id",
                destination,
                progress,
            )

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"telegram-file-id")

        client = FakeClient.instances[0]
        self.assertEqual(client.name, "telegram_bot_large_files")
        self.assertEqual(
            client.kwargs,
            {
                "api_id": 12345,
                "api_hash": "a" * 32,
                "bot_token": "123:bot-secret",
                "in_memory": True,
                "no_updates": True,
                "max_concurrent_transmissions": 1,
                "loop": asyncio.get_running_loop(),
            },
        )
        self.assertEqual(client.started, 1)
        self.assertEqual(progress_updates, [(4, 10)])

    async def test_sync_progress_callback_is_supported(self):
        progress_updates = []
        with TemporaryDirectory() as tmp:
            await self.downloader.download(
                "file-id",
                Path(tmp) / "large.pdf",
                lambda current, total: progress_updates.append(
                    (current, total)
                ),
            )

        self.assertEqual(progress_updates, [(4, 10)])

    async def test_transfers_are_serialized_and_client_is_reused(self):
        with TemporaryDirectory() as tmp:
            await asyncio.gather(
                self.downloader.download("one", Path(tmp) / "one.pdf"),
                self.downloader.download("two", Path(tmp) / "two.pdf"),
            )

        self.assertEqual(FakeClient.maximum_active_downloads, 1)
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(FakeClient.instances[0].started, 1)

    async def test_relative_destination_is_rejected_before_client_start(self):
        with self.assertRaisesRegex(MtprotoPathError, "absolute"):
            await self.downloader.download("file-id", "relative.pdf")

        self.assertEqual(FakeClient.instances, [])

    async def test_failed_transfer_preserves_existing_file_and_cleans_partials(self):
        class FailingClient(FakeClient):
            async def download_media(self, file_id, *, file_name, progress):
                Path(file_name).write_bytes(b"partial")
                Path(f"{file_name}.temp").write_bytes(b"library partial")
                raise RuntimeError("network failed")

        downloader = MtprotoDownloader(
            api_id=12345,
            api_hash="b" * 32,
            bot_token="token",
            client_factory=FailingClient,
        )
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "large.pdf"
            destination.write_bytes(b"original")

            with self.assertRaises(MtprotoDownloadError):
                await downloader.download("file-id", destination)

            self.assertEqual(destination.read_bytes(), b"original")
            self.assertEqual(list(Path(tmp).glob(".*.part*")), [])
        await downloader.close()

    async def test_close_stops_session_and_prevents_reuse(self):
        with TemporaryDirectory() as tmp:
            await self.downloader.download(
                "file-id",
                Path(tmp) / "large.pdf",
            )

        client = FakeClient.instances[0]
        await self.downloader.close()
        self.assertEqual(client.stopped, 1)

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(MtprotoConnectionError, "closed"):
                await self.downloader.download(
                    "file-id",
                    Path(tmp) / "other.pdf",
                )

    async def test_empty_file_id_is_rejected(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(MtprotoDownloadError):
                await self.downloader.download("", Path(tmp) / "large.pdf")

    async def test_partially_started_client_is_stopped_after_start_failure(self):
        class StartFailureClient(FakeClient):
            async def start(self):
                raise RuntimeError("login failed")

        downloader = MtprotoDownloader(
            api_id=12345,
            api_hash="b" * 32,
            bot_token="token",
            client_factory=StartFailureClient,
        )
        with TemporaryDirectory() as tmp:
            with self.assertRaises(MtprotoConnectionError):
                await downloader.download("file-id", Path(tmp) / "large.pdf")

        self.assertEqual(StartFailureClient.instances[-1].stopped, 1)
        await downloader.close()

    async def test_media_group_documents_are_exact_sorted_and_normalized(self):
        def document(file_id, name, mime_type="application/pdf"):
            return SimpleNamespace(
                file_id=file_id,
                file_unique_id=f"unique-{file_id}",
                file_name=name,
                mime_type=mime_type,
                file_size=123,
            )

        FakeClient.media_group_messages = [
            SimpleNamespace(
                id=12,
                media_group_id=700,
                document=document("third", "third.pdf"),
            ),
            SimpleNamespace(
                id=10,
                media_group_id=700,
                document=document("first", "first.pdf"),
            ),
            # A non-document member in the selected album is ignored.
            SimpleNamespace(id=11, media_group_id=700, document=None),
            # Defensive filtering must never include a neighboring album.
            SimpleNamespace(
                id=13,
                media_group_id=701,
                document=document("unrelated", "unrelated.pdf"),
            ),
        ]

        documents = await self.downloader.get_media_group_documents(-500, 10)

        self.assertEqual(FakeClient.media_group_calls, [(-500, 10)])
        self.assertEqual([item.message_id for item in documents], [10, 12])
        self.assertEqual([item.file_name for item in documents], [
            "first.pdf",
            "third.pdf",
        ])
        self.assertEqual(documents[0].media_group_id, "700")
        self.assertEqual(documents[0].file_unique_id, "unique-first")
        self.assertEqual(documents[0].file_size, 123)
        self.assertTrue(documents[0].mtproto_only)

    async def test_media_group_lookup_errors_are_typed(self):
        FakeClient.media_group_error = RuntimeError("history unavailable")

        with self.assertRaisesRegex(
            MtprotoMediaGroupError,
            "could not retrieve all items",
        ):
            await self.downloader.get_media_group_documents(-500, 10)

    async def test_media_group_requires_the_selected_message(self):
        FakeClient.media_group_messages = [
            SimpleNamespace(
                id=11,
                media_group_id=700,
                document=SimpleNamespace(
                    file_id="file-id",
                    file_unique_id="unique-id",
                    file_name="file.pdf",
                    mime_type="application/pdf",
                    file_size=123,
                ),
            )
        ]

        with self.assertRaisesRegex(
            MtprotoMediaGroupError,
            "selected album message",
        ):
            await self.downloader.get_media_group_documents(-500, 10)

    async def test_media_group_rejects_invalid_message_id_before_connecting(self):
        with self.assertRaisesRegex(MtprotoMediaGroupError, "positive"):
            await self.downloader.get_media_group_documents(-500, 0)

        self.assertEqual(FakeClient.instances, [])


class MtprotoLazyDependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_dependency_is_a_clear_typed_error(self):
        downloader = MtprotoDownloader(
            api_id=12345,
            api_hash="b" * 32,
            bot_token="token",
        )
        with (
            TemporaryDirectory() as tmp,
            patch(
                "bot.mtproto.importlib.import_module",
                side_effect=ModuleNotFoundError("pyrogram"),
            ),
            self.assertRaisesRegex(MtprotoDependencyError, "Kurigram"),
        ):
            await downloader.download("file-id", Path(tmp) / "large.pdf")
        await downloader.close()


if __name__ == "__main__":
    unittest.main()
