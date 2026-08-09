"""Download large Telegram files through a bot-authenticated MTProto client.

Kurigram is imported lazily so the rest of the bot can still start when
large-file support is not configured or its optional dependency is missing.
The distribution is named ``kurigram``, but intentionally retains Pyrogram's
``pyrogram`` import namespace and public ``Client.download_media`` API.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias
from uuid import uuid4


ProgressCallback: TypeAlias = Callable[
    [int, int],
    Awaitable[None] | None,
]


class MtprotoError(Exception):
    """Base class for recoverable MTProto large-file errors."""


class MtprotoConfigurationError(MtprotoError, ValueError):
    """The MTProto downloader was given incomplete credentials."""


class MtprotoDependencyError(MtprotoError, ImportError):
    """Kurigram is unavailable or could not be imported."""


class MtprotoConnectionError(MtprotoError):
    """The bot-authenticated MTProto client could not be started or stopped."""


class MtprotoPathError(MtprotoError, ValueError):
    """The requested download destination is unsafe or unusable."""


class MtprotoDownloadError(MtprotoError):
    """Telegram could not download the selected file over MTProto."""


class MtprotoMediaGroupError(MtprotoError):
    """Telegram could not enumerate the selected media group."""


@dataclass(frozen=True)
class MtprotoDocument:
    """Transport-neutral document metadata returned from an MTProto album."""

    message_id: int
    media_group_id: str
    file_id: str
    file_unique_id: str | None
    file_name: str | None
    mime_type: str | None
    file_size: int | None
    mtproto_only: bool = True


def _load_client_factory() -> Callable[..., Any]:
    """Load Kurigram's public Pyrogram-compatible Client lazily."""
    try:
        pyrogram = importlib.import_module("pyrogram")
        client = pyrogram.Client
    except Exception as exc:
        raise MtprotoDependencyError(
            "Large-file downloads require Kurigram. Install the project's "
            "requirements and restart the bot."
        ) from exc

    if not callable(client):
        raise MtprotoDependencyError(
            "The installed Kurigram package does not provide pyrogram.Client."
        )
    return client


class MtprotoDownloader:
    """A lazy, bot-only MTProto downloader with serialized transfers.

    One instance should be shared by the application. Its in-memory session is
    authenticated solely with the same bot token used by the hosted Bot API;
    it never asks for a phone number or creates a personal-user session file.
    """

    def __init__(
        self,
        *,
        api_id: int | str,
        api_hash: str,
        bot_token: str,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        try:
            parsed_api_id = int(api_id)
        except (TypeError, ValueError) as exc:
            raise MtprotoConfigurationError(
                "A numeric Telegram api_id is required for large files."
            ) from exc
        if isinstance(api_id, bool) or parsed_api_id <= 0:
            raise MtprotoConfigurationError(
                "A positive Telegram api_id is required for large files."
            )
        if not isinstance(api_hash, str) or not api_hash.strip():
            raise MtprotoConfigurationError(
                "A Telegram api_hash is required for large files."
            )
        if re.fullmatch(r"[0-9a-fA-F]{32}", api_hash.strip()) is None:
            raise MtprotoConfigurationError(
                "TELEGRAM_API_HASH must be the 32-character hexadecimal "
                "api_hash from my.telegram.org."
            )
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise MtprotoConfigurationError(
                "The bot token is required for large-file downloads."
            )

        self._api_id = parsed_api_id
        self._api_hash = api_hash.strip()
        self._bot_token = bot_token.strip()
        self._client_factory = client_factory
        self._client: Any | None = None
        self._started = False
        self._closed = False
        self._transfer_lock = asyncio.Lock()

    async def __aenter__(self) -> MtprotoDownloader:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def _start_client(self) -> Any:
        if self._closed:
            raise MtprotoConnectionError(
                "The MTProto downloader has already been closed."
            )
        if self._started:
            return self._client

        factory = self._client_factory or _load_client_factory()
        client = None
        try:
            client = factory(
                "telegram_bot_large_files",
                api_id=self._api_id,
                api_hash=self._api_hash,
                bot_token=self._bot_token,
                in_memory=True,
                no_updates=True,
                max_concurrent_transmissions=1,
                loop=asyncio.get_running_loop(),
            )
            await client.start()
        except MtprotoError:
            raise
        except Exception as exc:
            if client is not None:
                try:
                    await client.stop()
                except Exception:
                    pass
            self._client = None
            self._started = False
            raise MtprotoConnectionError(
                "Could not start the bot's MTProto large-file client."
            ) from exc

        self._client = client
        self._started = True
        return client

    @staticmethod
    def _destination_path(destination: str | os.PathLike[str]) -> Path:
        try:
            path = Path(destination)
        except TypeError as exc:
            raise MtprotoPathError(
                "The MTProto download destination must be a filesystem path."
            ) from exc
        if not path.is_absolute():
            raise MtprotoPathError(
                "The MTProto download destination must be an absolute path."
            )
        if path.exists() and path.is_dir():
            raise MtprotoPathError(
                "The MTProto download destination points to a directory."
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MtprotoPathError(
                "The MTProto download directory could not be created."
            ) from exc
        return path

    async def download(
        self,
        file_id: str,
        destination: str | os.PathLike[str],
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download ``file_id`` atomically to an absolute ``destination``.

        Concurrent calls on the same application-level instance are serialized
        to keep MTProto transfer state and bandwidth predictable. Both normal
        and async progress callbacks are supported.
        """
        if not isinstance(file_id, str) or not file_id.strip():
            raise MtprotoDownloadError(
                "Telegram did not provide a usable file_id for this document."
            )
        if progress is not None and not callable(progress):
            raise MtprotoDownloadError("The download progress callback is invalid.")

        target = self._destination_path(destination)
        temporary = target.with_name(
            f".{target.name}.{uuid4().hex}.part"
        )

        async def report_progress(current: int, total: int) -> None:
            if progress is None:
                return
            result = progress(current, total)
            if inspect.isawaitable(result):
                await result

        async with self._transfer_lock:
            client = await self._start_client()
            try:
                downloaded = await client.download_media(
                    file_id,
                    file_name=str(temporary),
                    progress=report_progress if progress is not None else None,
                )
                if downloaded is None:
                    raise MtprotoDownloadError(
                        "Telegram stopped the MTProto download before it completed."
                    )

                downloaded_path = Path(downloaded)
                if (
                    downloaded_path.resolve(strict=False)
                    != temporary.resolve(strict=False)
                ):
                    raise MtprotoDownloadError(
                        "Kurigram returned an unexpected download path."
                    )
                if not downloaded_path.is_file():
                    raise MtprotoDownloadError(
                        "Telegram reported a completed download, but no file was saved."
                    )
                os.replace(downloaded_path, target)
                return target
            except asyncio.CancelledError:
                raise
            except MtprotoError:
                raise
            except Exception as exc:
                raise MtprotoDownloadError(
                    "Telegram could not download the selected file over MTProto."
                ) from exc
            finally:
                for partial in (temporary, Path(f"{temporary}.temp")):
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        pass

    async def get_media_group_documents(
        self,
        chat_id: int | str,
        message_id: int,
    ) -> list[MtprotoDocument]:
        """Return document members of the exact album containing a message."""
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise MtprotoMediaGroupError(
                "The selected album message ID is invalid."
            )
        if message_id <= 0:
            raise MtprotoMediaGroupError(
                "The selected album message ID must be positive."
            )
        if not isinstance(chat_id, (int, str)) or isinstance(chat_id, bool):
            raise MtprotoMediaGroupError(
                "The selected album chat ID is invalid."
            )

        async with self._transfer_lock:
            client = await self._start_client()
            try:
                messages = await client.get_media_group(chat_id, message_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MtprotoMediaGroupError(
                    "Telegram could not retrieve all items in this album."
                ) from exc

        target_media_group_id = next(
            (
                str(getattr(message, "media_group_id"))
                for message in messages
                if getattr(message, "id", None) == message_id
                and getattr(message, "media_group_id", None)
            ),
            None,
        )
        if target_media_group_id is None:
            raise MtprotoMediaGroupError(
                "Telegram did not return the selected album message."
            )

        documents = {}
        for message in messages:
            document = getattr(message, "document", None)
            current_message_id = getattr(message, "id", None)
            media_group_id = getattr(message, "media_group_id", None)
            file_id = getattr(document, "file_id", None)
            if (
                document is None
                or not isinstance(current_message_id, int)
                or not media_group_id
                or str(media_group_id) != target_media_group_id
                or not file_id
            ):
                continue
            documents[current_message_id] = MtprotoDocument(
                message_id=current_message_id,
                media_group_id=str(media_group_id),
                file_id=file_id,
                file_unique_id=getattr(document, "file_unique_id", None),
                file_name=getattr(document, "file_name", None),
                mime_type=getattr(document, "mime_type", None),
                file_size=getattr(document, "file_size", None),
            )
        return [documents[key] for key in sorted(documents)]

    async def close(self) -> None:
        """Stop the in-memory bot session and permanently close this instance."""
        async with self._transfer_lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
            was_started = self._started
            self._started = False
            if client is None or not was_started:
                return
            try:
                await client.stop()
            except Exception as exc:
                raise MtprotoConnectionError(
                    "Could not stop the bot's MTProto large-file client cleanly."
                ) from exc


__all__ = [
    "MtprotoConfigurationError",
    "MtprotoConnectionError",
    "MtprotoDependencyError",
    "MtprotoDownloadError",
    "MtprotoDocument",
    "MtprotoDownloader",
    "MtprotoError",
    "MtprotoMediaGroupError",
    "MtprotoPathError",
    "ProgressCallback",
]
