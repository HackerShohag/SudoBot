import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot import status_animation


class StatusAnimationTests(unittest.IsolatedAsyncioTestCase):
    def test_status_frames_are_plain_text_and_cycle(self):
        first = status_animation.animated_status_text("Working…", 0)
        cycled = status_animation.animated_status_text(
            "Working…",
            len(status_animation.STATUS_FRAMES),
        )

        self.assertEqual(first, cycled)
        self.assertTrue(first.endswith(" Working…"))

    async def test_animation_edits_frames_until_cancelled(self):
        edit = AsyncMock()
        real_sleep = asyncio.sleep

        async def yield_once(_delay):
            await real_sleep(0)

        with patch.object(
            status_animation.asyncio,
            "sleep",
            side_effect=yield_once,
        ):
            task = asyncio.create_task(
                status_animation.animate_status("Working…", edit)
            )
            while edit.await_count < 2:
                await real_sleep(0)
            await status_animation.stop_animation(task)

        self.assertGreaterEqual(edit.await_count, 2)
        self.assertNotEqual(
            edit.await_args_list[0].args[0],
            edit.await_args_list[1].args[0],
        )


if __name__ == "__main__":
    unittest.main()
