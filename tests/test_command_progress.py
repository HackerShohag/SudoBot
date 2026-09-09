import asyncio
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from telegram.error import RetryAfter

import bot.bot as command_bot
from bot import task_registry


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


class TaskTrackingApplication:
    """Small stand-in for PTB's synchronous Application.create_task API."""

    def __init__(self):
        self.tasks = []

    def create_task(self, coroutine, *, update=None, name=None):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        return task

    async def wait_for_all(self):
        while True:
            pending = [task for task in self.tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)


def command_objects(*, chat_id=100, application=None):
    application = application or TaskTrackingApplication()
    status_message = SimpleNamespace(message_id=11, edit_text=AsyncMock())
    user = SimpleNamespace(id=1, username="hackershohag")
    message = SimpleNamespace(
        from_user=user,
        chat_id=chat_id,
        message_id=10,
        text="/run test",
        reply_text=AsyncMock(return_value=status_message),
        delete=AsyncMock(),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=user,
    )
    context = SimpleNamespace(
        args=[],
        user_data={},
        chat_data={},
        application=application,
        bot=SimpleNamespace(
            delete_message=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )
    return update, context, status_message


class CommandProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        command_bot._active_commands.clear()
        task_registry._CHAT_TASKS.clear()

    async def asyncTearDown(self):
        tasks = []
        for commands in list(command_bot._active_commands.values()):
            for active in list(commands.values()):
                active.stop_requested = True
                if active.task is not None and not active.task.done():
                    active.task.cancel()
                    tasks.append(active.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        command_bot._active_commands.clear()
        registry_tasks = [
            task
            for tracked in task_registry._CHAT_TASKS.values()
            for task in tracked
            if not task.done()
        ]
        for task in registry_tasks:
            task.cancel()
        if registry_tasks:
            await asyncio.gather(*registry_tasks, return_exceptions=True)
        task_registry._CHAT_TASKS.clear()

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
        self.assertEqual(final_text.count("(no output)"), 1)
        self.assertNotIn("stderr:", final_text)
        self.assertNotIn(("chat", 100), command_bot._active_commands)

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

    async def test_running_a_command_refreshes_the_history_keyboard(self):
        update, context, _status_message = command_objects()
        process = FakeProcess()

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch("bot.bot.update_keyboard", new_callable=AsyncMock) as keyboard,
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(return_value=process),
            ),
        ):
            await command_bot.execute_command("true", update, context, 10)

        keyboard.assert_awaited_once_with(update, context)

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

    async def test_arbitrary_long_output_is_copyable_html_and_within_limit(self):
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
        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;�", final_text)
        self.assertIn("stdout:\n<pre>", final_text)
        self.assertNotIn("stderr:\n<pre>", final_text)
        self.assertIn("earlier output omitted", final_text)
        self.assertEqual(final_call.kwargs["parse_mode"], "HTML")

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
            await context.application.wait_for_all()

        create_process.assert_awaited_once_with(
            "cat uploads/input.txt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=ANY,
        )
        update.message.reply_text.assert_awaited_once()
        edits = [call.args[0] for call in status_message.edit_text.await_args_list]
        self.assertTrue(any("Running file command" in text for text in edits[:-1]))
        self.assertIn("✅ File command completed", edits[-1])
        self.assertIn("file output", edits[-1])

    async def test_commands_in_the_same_chat_run_in_parallel(self):
        application = TaskTrackingApplication()
        update_a, context_a, _ = command_objects(application=application)
        update_b, context_b, _ = command_objects(application=application)
        context_a.args = ["first"]
        context_b.args = ["second"]
        processes = [FakeProcess(delay=0.02), FakeProcess(delay=0.02)]

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(side_effect=processes),
            ) as create_process,
        ):
            await command_bot.run_command(update_a, context_a)
            await command_bot.run_command(update_b, context_b)
            self.assertEqual(
                len(command_bot._commands_for_chat(("chat", 100))),
                2,
            )
            await asyncio.wait_for(application.wait_for_all(), timeout=1)

        self.assertEqual(create_process.await_count, 2)
        self.assertEqual(command_bot._active_commands, {})

    async def test_run_returns_immediately_and_stop_interrupts_active_process(self):
        application = TaskTrackingApplication()
        run_update, context, status_message = command_objects(
            application=application
        )
        stop_update, stop_context, _ = command_objects(
            application=application
        )
        context.args = ["long-running"]
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
            await asyncio.wait_for(
                command_bot.run_command(run_update, context),
                timeout=0.1,
            )
            active = command_bot._commands_for_chat(("chat", 100))[0]
            self.assertIsNotNone(active.task)
            self.assertFalse(active.task.done())

            for _ in range(50):
                if active.process is process:
                    break
                await asyncio.sleep(0)
            self.assertIs(active.process, process)

            await command_bot.stop_command(stop_update, stop_context)
            await asyncio.wait_for(application.wait_for_all(), timeout=1)

        stop_update.message.reply_text.assert_awaited_once_with(
            "🛑 Stopping all running work in this chat (1 task)."
        )
        self.assertIn(signal.SIGTERM, process.signals)
        final_text = status_message.edit_text.await_args_list[-1].args[0]
        self.assertIn("🛑 Command stopped", final_text)
        self.assertNotIn(("chat", 100), command_bot._active_commands)

    async def test_stop_interrupts_all_parallel_commands_in_the_chat(self):
        application = TaskTrackingApplication()
        update_a, context_a, _ = command_objects(application=application)
        update_b, context_b, _ = command_objects(application=application)
        stop_update, stop_context, _ = command_objects(application=application)
        context_a.args = ["first"]
        context_b.args = ["second"]
        process_a = StoppableFakeProcess()
        process_b = StoppableFakeProcess()

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(side_effect=[process_a, process_b]),
            ),
        ):
            await command_bot.run_command(update_a, context_a)
            await command_bot.run_command(update_b, context_b)
            for _ in range(50):
                if process_a.returncode is None and process_b.returncode is None:
                    active = command_bot._commands_for_chat(("chat", 100))
                    if len(active) == 2 and all(item.process for item in active):
                        break
                await asyncio.sleep(0)

            await command_bot.stop_command(stop_update, stop_context)
            await asyncio.wait_for(application.wait_for_all(), timeout=1)

        self.assertIn(signal.SIGTERM, process_a.signals)
        self.assertIn(signal.SIGTERM, process_b.signals)
        stop_update.message.reply_text.assert_awaited_once_with(
            "🛑 Stopping all running work in this chat (2 tasks)."
        )
        self.assertEqual(command_bot._active_commands, {})

    async def test_immediate_stop_before_runner_starts_prevents_subprocess_spawn(self):
        application = TaskTrackingApplication()
        run_update, run_context, _ = command_objects(application=application)
        stop_update, stop_context, _ = command_objects(application=application)
        run_context.args = ["must-not-start"]

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as create_process,
        ):
            # Neither callback yields on this path. /stop therefore marks the
            # reserved slot before the newly scheduled runner gets a turn.
            await command_bot.run_command(run_update, run_context)
            active = command_bot._commands_for_chat(("chat", 100))[0]
            self.assertIsNone(active.process)
            await command_bot.stop_command(stop_update, stop_context)
            await asyncio.wait_for(application.wait_for_all(), timeout=1)

        create_process.assert_not_awaited()
        stop_update.message.reply_text.assert_awaited_once_with(
            "🛑 Stopping all running work in this chat (1 task)."
        )
        self.assertNotIn(("chat", 100), command_bot._active_commands)

    async def test_stop_is_scoped_to_the_chat_that_requested_it(self):
        application = TaskTrackingApplication()
        update_a, context_a, _ = command_objects(
            chat_id=100,
            application=application,
        )
        update_b, context_b, _ = command_objects(
            chat_id=200,
            application=application,
        )
        unrelated_stop, unrelated_context, _ = command_objects(
            chat_id=300,
            application=application,
        )
        stop_a, stop_context_a, _ = command_objects(
            chat_id=100,
            application=application,
        )
        stop_b, stop_context_b, _ = command_objects(
            chat_id=200,
            application=application,
        )
        context_a.args = ["command-a"]
        context_b.args = ["command-b"]
        process_a = StoppableFakeProcess()
        process_b = StoppableFakeProcess()

        with (
            patch("bot.bot.is_super_admin", return_value=True),
            patch("bot.bot.update_command_history"),
            patch(
                "bot.bot.asyncio.create_subprocess_shell",
                new=AsyncMock(side_effect=[process_a, process_b]),
            ),
        ):
            await command_bot.run_command(update_a, context_a)
            await command_bot.run_command(update_b, context_b)

            for _ in range(50):
                commands_a = command_bot._commands_for_chat(("chat", 100))
                commands_b = command_bot._commands_for_chat(("chat", 200))
                active_a = commands_a[0] if commands_a else None
                active_b = commands_b[0] if commands_b else None
                if (
                    active_a is not None
                    and active_a.process is process_a
                    and active_b is not None
                    and active_b.process is process_b
                ):
                    break
                await asyncio.sleep(0)
            self.assertIs(active_a.process, process_a)
            self.assertIs(active_b.process, process_b)

            await command_bot.stop_command(unrelated_stop, unrelated_context)
            self.assertEqual(process_a.signals, [])
            self.assertEqual(process_b.signals, [])
            unrelated_stop.message.reply_text.assert_awaited_once_with(
                "⚠️ No running tasks were found in this chat."
            )

            await command_bot.stop_command(stop_a, stop_context_a)
            self.assertIn(signal.SIGTERM, process_a.signals)
            self.assertEqual(process_b.signals, [])

            await asyncio.wait_for(
                asyncio.gather(
                    active_a.task,
                    return_exceptions=True,
                ),
                timeout=1,
            )
            self.assertNotIn(("chat", 100), command_bot._active_commands)
            self.assertIn(("chat", 200), command_bot._active_commands)

            await command_bot.stop_command(stop_b, stop_context_b)
            await asyncio.wait_for(application.wait_for_all(), timeout=1)

        self.assertIn(signal.SIGTERM, process_b.signals)
        self.assertEqual(command_bot._active_commands, {})

    async def test_sudo_prompt_and_stop_state_are_scoped_by_chat(self):
        application = TaskTrackingApplication()
        update_a, context_a, _ = command_objects(
            chat_id=100,
            application=application,
        )
        update_b, context_b, _ = command_objects(
            chat_id=200,
            application=application,
        )
        stop_a, stop_context_a, _ = command_objects(
            chat_id=100,
            application=application,
        )
        context_a.args = ["sudo", "whoami"]
        context_b.args = ["sudo", "id"]
        stop_context_a.chat_data = context_a.chat_data

        with patch("bot.bot.is_super_admin", return_value=True):
            await command_bot.run_command(update_a, context_a)
            await command_bot.run_command(update_b, context_b)

            self.assertEqual(context_a.chat_data["sudo_command"], "sudo whoami")
            self.assertEqual(context_b.chat_data["sudo_command"], "sudo id")

            await command_bot.stop_command(stop_a, stop_context_a)

        self.assertEqual(context_a.chat_data, {})
        self.assertEqual(context_b.chat_data["sudo_command"], "sudo id")

    async def test_stop_cancels_all_tracked_work_in_chat_but_not_other_chat(self):
        update, context, _ = command_objects(chat_id=100)
        other_update, _, _ = command_objects(chat_id=200)
        same_chat_started = [asyncio.Event(), asyncio.Event()]
        other_chat_started = asyncio.Event()
        cleanup_seen = [asyncio.Event(), asyncio.Event()]

        async def work(started, cleaned=None):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                if cleaned is not None:
                    cleaned.set()

        same_chat_tasks = [
            asyncio.create_task(work(started, cleaned))
            for started, cleaned in zip(same_chat_started, cleanup_seen)
        ]
        other_chat_task = asyncio.create_task(work(other_chat_started))
        task_registry.register_task(update, same_chat_tasks[0], "PDF processing")
        task_registry.register_task(update, same_chat_tasks[1], "System monitor")
        task_registry.register_task(other_update, other_chat_task, "System usage")
        await asyncio.gather(
            *(event.wait() for event in same_chat_started),
            other_chat_started.wait(),
        )

        with patch("bot.bot.is_super_admin", return_value=True):
            await command_bot.stop_command(update, context)
        await asyncio.gather(*same_chat_tasks, return_exceptions=True)

        self.assertTrue(all(task.cancelled() for task in same_chat_tasks))
        self.assertTrue(all(event.is_set() for event in cleanup_seen))
        self.assertFalse(other_chat_task.done())
        update.message.reply_text.assert_awaited_once_with(
            "🛑 Stopping all running work in this chat (2 tasks)."
        )
        self.assertEqual(task_registry.tracked_tasks(update), {})
        self.assertEqual(
            task_registry.tracked_tasks(other_update),
            {other_chat_task: "System usage"},
        )

        other_chat_task.cancel()
        await asyncio.gather(other_chat_task, return_exceptions=True)

    async def test_second_stop_does_not_cancel_cleaning_task_again(self):
        update, context, _ = command_objects(chat_id=100)
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def work():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                raise

        task = asyncio.create_task(work())
        task_registry.register_task(update, task, "PDF processing")
        await asyncio.sleep(0)

        with patch("bot.bot.is_super_admin", return_value=True):
            await command_bot.stop_command(update, context)
            await cleanup_started.wait()
            await command_bot.stop_command(update, context)

        self.assertEqual(task.cancelling(), 1)
        self.assertEqual(
            [call.args[0] for call in update.message.reply_text.await_args_list],
            [
                "🛑 Stopping all running work in this chat (1 task).",
                "🛑 Running work in this chat is already stopping.",
            ],
        )

        cleanup_release.set()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
