import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter

import bot.bot as command_bot


class FakeStream:
    def __init__(self, chunks=(), *, coordinate_with=None, started=None):
        self._chunks = list(chunks)
        self._coordinate_with = coordinate_with
        self._started = started

    async def read(self, _size):
        if self._started is not None:
            self._started.set()
        if self._coordinate_with is not None:
            await self._coordinate_with.wait()
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeProcess:
    def __init__(self, stdout=(), stderr=(), *, return_code=0, delay=0):
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = None
        self._return_code = return_code
        self._delay = delay
        self.signals = []

    async def wait(self):
        if self.returncode is None:
            await asyncio.sleep(self._delay)
            self.returncode = self._return_code
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = -value

    def kill(self):
        self.returncode = -9


class StoppableFakeProcess(FakeProcess):
    def __init__(self):
        super().__init__()
        self._finished = asyncio.Event()

    async def wait(self):
        await self._finished.wait()
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = -value
        self._finished.set()


def command_objects():
    status_message = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="hackershohag"),
        message_id=10,
        text="/run test",
        reply_text=AsyncMock(return_value=status_message),
    )
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(args=[], user_data={})
    return update, context, status_message


class CommandProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        command_bot.running_process = None
        command_bot._active_command = None
        command_bot._command_starting = False

    async def test_no_output_finishes_in_the_original_status_message(self):
        update, context, status_message = command_objects()
        process = FakeProcess()

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ),
        ):
            await command_bot.execute_command("true", update, context, 10)

        update.message.reply_text.assert_awaited_once()
        final_text = status_message.edit_text.await_args_list[-1].args[0]
        self.assertIn("✅ Command completed", final_text)
        self.assertIn("Exit code: 0", final_text)
        self.assertEqual(final_text.count("(no output)"), 2)
        self.assertIsNone(command_bot.running_process)

    async def test_stdout_and_stderr_are_drained_concurrently(self):
        update, context, status_message = command_objects()
        stdout_started = asyncio.Event()
        stderr_started = asyncio.Event()
        process = FakeProcess(return_code=7)
        process.stdout = FakeStream(
            [b"normal output\n"],
            coordinate_with=stderr_started,
            started=stdout_started,
        )
        process.stderr = FakeStream(
            [b"error output\n"],
            coordinate_with=stdout_started,
            started=stderr_started,
        )

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ),
        ):
            await asyncio.wait_for(
                command_bot.execute_command("both-streams", update, context, 10),
                timeout=1,
            )

        final_text = status_message.edit_text.await_args_list[-1].args[0]
        self.assertIn("❌ Command failed", final_text)
        self.assertIn("normal output", final_text)
        self.assertIn("error output", final_text)

    async def test_long_running_command_edits_progress_then_final_result(self):
        update, context, status_message = command_objects()
        process = FakeProcess(stdout=[b"working\n"], delay=0.04)

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch("bot.bot.STATUS_EDIT_INTERVAL", 0.01),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ),
        ):
            await command_bot.execute_command("slow", update, context, 10)

        edits = [call.args[0] for call in status_message.edit_text.await_args_list]
        self.assertTrue(any("Running command" in text for text in edits[:-1]))
        self.assertIn("✅ Command completed", edits[-1])
        self.assertIn("working", edits[-1])
        update.message.reply_text.assert_awaited_once()

    async def test_arbitrary_long_output_is_plain_text_and_within_limit(self):
        update, context, status_message = command_objects()
        output = (b"x" * 10_000) + b"\x1b[31m<b>unsafe</b>\x00"
        process = FakeProcess(stdout=[output])

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ),
        ):
            await command_bot.execute_command("large", update, context, 10)

        final_call = status_message.edit_text.await_args_list[-1]
        final_text = final_call.args[0]
        self.assertLessEqual(len(final_text), 4096)
        self.assertIn("<b>unsafe</b>�", final_text)
        self.assertIn("earlier output omitted", final_text)
        self.assertNotIn("parse_mode", final_call.kwargs)

    async def test_status_edit_honors_retry_after_and_skips_identical_text(self):
        message = SimpleNamespace(
            edit_text=AsyncMock(side_effect=[RetryAfter(1), None])
        )
        status = command_bot._StatusMessage(message=message, last_text="old")

        with patch("bot.bot.asyncio.sleep", new=AsyncMock()) as sleep:
            changed = await command_bot._edit_status(status, "new")

        self.assertTrue(changed)
        self.assertEqual(message.edit_text.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

        await command_bot._edit_status(status, "new")
        self.assertEqual(message.edit_text.await_count, 2)

    async def test_runfile_uses_streaming_async_status_engine(self):
        update, context, status_message = command_objects()
        context.args = ["input.txt", "cat", "{file}"]
        process = FakeProcess(stdout=[b"file output"], delay=0.04)

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.os.path.exists", return_value=True),
            patch("bot.bot.update_command_history"),
            patch("bot.bot.STATUS_EDIT_INTERVAL", 0.01),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ) as create_process,
        ):
            await command_bot.execute_on_file(update, context)

        create_process.assert_awaited_once_with(
            "cat uploads/input.txt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        update.message.reply_text.assert_awaited_once()
        edits = [call.args[0] for call in status_message.edit_text.await_args_list]
        self.assertTrue(any("Running file command" in text for text in edits[:-1]))
        self.assertIn("✅ File command completed", edits[-1])
        self.assertIn("file output", edits[-1])

    async def test_second_command_is_rejected_without_overwriting_active_process(self):
        update, context, _status_message = command_objects()
        active_process = FakeProcess(delay=10)
        command_bot.running_process = active_process
        command_bot._active_command = SimpleNamespace(process=active_process)

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as create_process,
        ):
            await command_bot.execute_command("second", update, context, 10)

        create_process.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "⚠️ Another host command is already running. Stop it with /stop first.",
            reply_to_message_id=10,
        )
        self.assertIs(command_bot.running_process, active_process)

    async def test_stop_finishes_by_editing_the_active_status_only(self):
        run_update, context, status_message = command_objects()
        stop_update, stop_context, _ = command_objects()
        process = StoppableFakeProcess()

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch("bot.bot.STATUS_EDIT_INTERVAL", 0.01),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ),
        ):
            command_task = asyncio.create_task(
                command_bot.execute_command("long-running", run_update, context, 10)
            )
            for _ in range(20):
                if command_bot.running_process is process:
                    break
                await asyncio.sleep(0)
            await command_bot.stop_command(stop_update, stop_context)
            await asyncio.wait_for(command_task, timeout=1)

        stop_update.message.reply_text.assert_not_awaited()
        final_text = status_message.edit_text.await_args_list[-1].args[0]
        self.assertIn("🛑 Command stopped", final_text)
        self.assertIsNone(command_bot.running_process)


if __name__ == "__main__":
    unittest.main()
