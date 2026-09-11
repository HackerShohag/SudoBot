"""Shared, rate-safe text animation for Telegram status messages."""

import asyncio
import logging


logger = logging.getLogger(__name__)

STATUS_FRAMES = (
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
)
STATUS_ANIMATION_INTERVAL = 3.0


def animated_status_text(text: str, frame: int = 0) -> str:
    """Prefix active work with a restrained, non-emoji spinner frame."""
    return f"{STATUS_FRAMES[frame % len(STATUS_FRAMES)]} {text}"


async def animate_status(
    text: str,
    edit,
    *,
    interval: float = STATUS_ANIMATION_INTERVAL,
) -> None:
    """Keep editing one message until the owner cancels this task."""
    frame = 1
    while True:
        await asyncio.sleep(interval)
        try:
            await edit(animated_status_text(text, frame))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Animation is decorative and must never stop the real work.
            logger.debug("Status animation edit failed", exc_info=True)
        frame += 1


async def stop_animation(task) -> None:
    """Cancel an animation without leaking its cancellation to the caller."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
