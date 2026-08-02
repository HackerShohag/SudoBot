import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bot.mtproto import (
    MtprotoConfigurationError,
    MtprotoConnectionError,
    MtprotoDependencyError,
    MtprotoDownloadError,
    MtprotoDownloader,
    MtprotoPathError,
)


class FakeClient:
    instances = []
    active_downloads = 0
    maximum_active_downloads = 0

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
