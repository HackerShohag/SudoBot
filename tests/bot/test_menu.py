import asyncio
import io
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import NetworkError, RetryAfter

from bot import menu
from bot import task_registry


def make_update(status=None, *, chat_id=100, user_id=1):
    status = status or SimpleNamespace(edit_text=AsyncMock())
    user = SimpleNamespace(id=user_id, username="testuser")
    message = SimpleNamespace(
        from_user=user,
        chat_id=chat_id,
        reply_text=AsyncMock(return_value=status),
    )
    return SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=user,
    ), status


class TestMenu(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        menu._ACTIVE_MONITORS.clear()
        task_registry._CHAT_TASKS.clear()

    async def asyncTearDown(self):
        pending = [
            task
            for tasks in task_registry._CHAT_TASKS.values()
            for task in tasks
            if isinstance(task, asyncio.Future) and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        menu._ACTIVE_MONITORS.clear()
        task_registry._CHAT_TASKS.clear()

    def test_cpu_name_falls_back_to_proc_cpuinfo(self):
        uname = SimpleNamespace(
            system="Linux",
            node="host",
            release="1",
            version="v1",
            machine="x86_64",
            processor="",
        )
        cpuinfo = io.StringIO(
            "vendor_id : AuthenticAMD\n"
            "model name : AMD Ryzen 7 4800H with Radeon Graphics\n"
            "cpu MHz : 2900.000\n"
        )

        with (
            patch("bot.menu.platform.uname", return_value=uname),
            patch("bot.menu.platform.system", return_value="Linux"),
            patch("bot.menu.platform.processor", return_value=""),
            patch("builtins.open", return_value=cpuinfo),
            patch("bot.menu.psutil.cpu_freq", return_value=None),
            patch("bot.menu.psutil.cpu_count", return_value=16),
        ):
            details = menu._collect_cpu_details()

        self.assertEqual(details["name"], "AMD Ryzen 7 4800H with Radeon Graphics")
        self.assertEqual(details["vendor"], "AuthenticAMD")
        self.assertEqual(details["speed_mhz"], "2900.000")

    def test_machine_specs_keep_cpu_and_ram_when_gpu_probe_fails(self):
        cpu = {
            "name": "AMD Ryzen 7 4800H",
            "architecture": "x86_64",
            "logical_cpus": 16,
            "speed_mhz": "2900",
            "vendor": "AuthenticAMD",
        }
        memory = SimpleNamespace(total=32 * menu.GIB)

        with (
            patch("bot.menu._collect_cpu_details", return_value=cpu),
            patch("bot.menu.psutil.virtual_memory", return_value=memory),
            patch("bot.menu.GPUtil.getGPUs", side_effect=ValueError("nvidia-smi failed")),
        ):
            response = menu._collect_machine_specs()

        self.assertIn("CPU: AMD Ryzen 7 4800H", response)
        self.assertIn("RAM Total: 32.0 GiB", response)
        self.assertIn("RAM DIMMs: Unavailable", response)
        self.assertIn("NVIDIA telemetry failed", response)

    def test_gpu_usage_uses_core_load_not_memory_utilization(self):
        gpu = SimpleNamespace(
            load=0.37,
            memoryUtil=0.82,
            memoryUsed=512,
            memoryTotal=4096,
        )

        with patch("bot.menu.GPUtil.getGPUs", return_value=[gpu]):
            response = menu._format_gpu_usage()

        self.assertIn("37.0%", response)
        self.assertNotIn("82.0%", response)

    def test_gpu_specs_label_driver_and_nvidia_scope(self):
        gpu = SimpleNamespace(name="RTX 3050", memoryTotal=4096, driver="580.95")
        with patch("bot.menu.GPUtil.getGPUs", return_value=[gpu]):
            response = menu._format_gpu_specs()

        self.assertIn("Driver: 580.95", response)
        self.assertNotIn("Vendor: 580.95", response)

        with patch("bot.menu.GPUtil.getGPUs", return_value=[]):
            response = menu._format_gpu_specs()
        self.assertIn("does not report non-NVIDIA GPUs", response)

    def test_public_ip_request_has_timeout_and_validates_response(self):
        response = Mock()
        response.json.return_value = {"ip": "203.0.113.10"}

        with patch("bot.menu.requests.get", return_value=response) as request:
            public_ip = menu._fetch_public_ip()

        self.assertEqual(public_ip, "203.0.113.10")
        request.assert_called_once_with(
            "https://api.ipify.org?format=json",
            timeout=menu.PUBLIC_IP_TIMEOUT_SECONDS,
        )
        response.raise_for_status.assert_called_once_with()

    async def test_bot_menu_includes_access_control_commands(self):
        application = SimpleNamespace(
            bot=SimpleNamespace(set_my_commands=AsyncMock())
        )

        await menu.set_bot_menu(application)

        commands = {
            command.command
            for command in application.bot.set_my_commands.await_args.args[0]
        }
        self.assertIn("authorize", commands)
        self.assertIn("unauthorize", commands)
        self.assertIn("get", commands)

    async def test_machine_specs_edits_one_progress_message(self):
        update, status = make_update()
        expected = "machine specs"

        with (
            patch("bot.menu.is_user_authorized", return_value=True),
            patch("bot.menu.asyncio.to_thread", new=AsyncMock(return_value=expected)) as to_thread,
        ):
            await menu.get_machine_specs(update, AsyncMock())

        to_thread.assert_awaited_once_with(menu._collect_machine_specs)
        update.message.reply_text.assert_awaited_once_with(
            menu.animated_status_text("Collecting machine specifications…")
        )
        status.edit_text.assert_awaited_once_with(expected)

    async def test_machine_specs_preserves_authorization(self):
        update, _ = make_update()

        with patch("bot.menu.is_user_authorized", return_value=False):
            await menu.get_machine_specs(update, AsyncMock())

        update.message.reply_text.assert_awaited_once_with(
            "❌ You are not authorized to run commands."
        )

    async def test_retry_after_waits_before_retrying_edit(self):
        status = SimpleNamespace(
            edit_text=AsyncMock(side_effect=[RetryAfter(timedelta(seconds=2)), None])
        )

        with patch("bot.menu.asyncio.sleep", new=AsyncMock()) as sleep:
            edited = await menu._edit_message_with_retry(status, "updated")

        self.assertTrue(edited)
        self.assertEqual(status.edit_text.await_count, 2)
        sleep.assert_awaited_once_with(2.0)

    async def test_network_error_waits_before_retrying_edit(self):
        status = SimpleNamespace(
            edit_text=AsyncMock(side_effect=[NetworkError("temporary failure"), None])
        )

        with patch("bot.menu.asyncio.sleep", new=AsyncMock()) as sleep:
            edited = await menu._edit_message_with_retry(status, "updated")

        self.assertTrue(edited)
        self.assertEqual(status.edit_text.await_count, 2)
        sleep.assert_awaited_once_with(menu.NETWORK_EDIT_RETRY_SECONDS)

    async def test_monitor_starts_one_background_task(self):
        update, status = make_update()
        task = Mock()
        task.done.return_value = False
        create_task = Mock(return_value=task)
        context = SimpleNamespace(application=SimpleNamespace(create_task=create_task))

        with patch("bot.menu.is_user_authorized", return_value=True):
            await menu.monitor_system_usage(update, context)

        update.message.reply_text.assert_awaited_once_with(
            menu.animated_status_text(
                "Starting system monitor — gathering the first reading…"
            )
        )
        context.application.create_task.assert_called_once()
        monitor_coroutine = context.application.create_task.call_args.args[0]
        self.assertTrue(asyncio.iscoroutine(monitor_coroutine))
        self.assertEqual(task.add_done_callback.call_count, 2)
        self.assertEqual(
            task_registry.tracked_tasks(update),
            {task: "System monitor"},
        )
        monitor_coroutine.close()

    async def test_monitor_rejects_a_second_task_for_same_chat_and_user(self):
        update, _ = make_update()
        task = Mock()
        task.done.return_value = False
        create_task = Mock(return_value=task)
        context = SimpleNamespace(application=SimpleNamespace(create_task=create_task))

        with patch("bot.menu.is_user_authorized", return_value=True):
            await menu.monitor_system_usage(update, context)
            await menu.monitor_system_usage(update, context)

        create_task.assert_called_once()
        self.assertEqual(update.message.reply_text.await_count, 2)
        self.assertEqual(
            update.message.reply_text.await_args_list[1].args[0],
            "🟢 System monitoring is already running in this chat for you.",
        )
        monitor_coroutine = create_task.call_args.args[0]
        monitor_coroutine.close()

    async def test_one_shot_status_command_is_cancellable_in_its_chat(self):
        update, status = make_update()
        collector_started = asyncio.Event()

        async def blocked_to_thread(_collector):
            collector_started.set()
            await asyncio.Event().wait()

        with (
            patch("bot.menu.is_user_authorized", return_value=True),
            patch("bot.menu.asyncio.to_thread", side_effect=blocked_to_thread),
        ):
            task = asyncio.create_task(menu.get_system_usage(update, None))
            await asyncio.wait_for(collector_started.wait(), timeout=1)
            self.assertEqual(
                task_registry.tracked_tasks(update),
                {task: "System usage check"},
            )

            cancelled = task_registry.cancel_chat_tasks(update)
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(cancelled, [(task, "System usage check")])
        self.assertTrue(task.cancelled())
        self.assertEqual(task_registry.tracked_tasks(update), {})
        self.assertEqual(
            status.edit_text.await_args_list[-1].args[0],
            "🛑 System usage check stopped.",
        )

    async def test_monitor_cancellation_updates_same_status_and_cleans_registry(self):
        update, status = make_update()
        monitor_started = asyncio.Event()

        async def blocked_monitor(_message):
            monitor_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await status.edit_text("🛑 System monitoring stopped.")
                raise

        class Application:
            @staticmethod
            def create_task(coroutine):
                return asyncio.create_task(coroutine)

        context = SimpleNamespace(application=Application())
        with (
            patch("bot.menu.is_user_authorized", return_value=True),
            patch("bot.menu._run_usage_monitor", side_effect=blocked_monitor),
        ):
            await menu.monitor_system_usage(update, context)
            monitor_task = menu._ACTIVE_MONITORS[(100, 1)]
            await asyncio.wait_for(monitor_started.wait(), timeout=1)

            cancelled = task_registry.cancel_chat_tasks(update)
            await asyncio.gather(monitor_task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertEqual(cancelled, [(monitor_task, "System monitor")])
        self.assertTrue(monitor_task.cancelled())
        self.assertEqual(menu._ACTIVE_MONITORS, {})
        self.assertEqual(task_registry.tracked_tasks(update), {})
        status.edit_text.assert_awaited_once_with(
            "🛑 System monitoring stopped."
        )

    async def test_usage_monitor_cancellation_edits_status_and_reraises(self):
        status = SimpleNamespace(edit_text=AsyncMock())
        collection_started = asyncio.Event()

        async def blocked_to_thread(_collector, _interval):
            collection_started.set()
            await asyncio.Event().wait()

        with patch(
            "bot.menu.asyncio.to_thread",
            side_effect=blocked_to_thread,
        ):
            task = asyncio.create_task(menu._run_usage_monitor(status))
            await asyncio.wait_for(collection_started.wait(), timeout=1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(task.cancelled())
        status.edit_text.assert_awaited_once_with(
            "🛑 System monitoring stopped."
        )

    async def test_monitor_uses_anchored_one_second_update_cadence(self):
        class Clock:
            value = 0.0

            def time(self):
                return self.value

        clock = Clock()
        edit_times = []
        rendered = []
        sleep_delays = []
        message = SimpleNamespace()

        async def fake_to_thread(_collector, _interval):
            clock.value += 0.2
            return "usage"

        async def fake_edit(_message, text):
            edit_times.append(clock.value)
            rendered.append(text)
            return True

        async def fake_sleep(delay):
            sleep_delays.append(delay)
            clock.value += delay

        with (
            patch(
                "bot.menu.asyncio.get_running_loop",
                return_value=SimpleNamespace(time=clock.time),
            ),
            patch("bot.menu.asyncio.to_thread", side_effect=fake_to_thread),
            patch("bot.menu.asyncio.sleep", side_effect=fake_sleep),
            patch("bot.menu._edit_message_with_retry", side_effect=fake_edit),
            patch("bot.menu.MONITOR_DURATION_SECONDS", 2.1),
            patch("bot.menu.MONITOR_INTERVAL_SECONDS", 1.0),
        ):
            await menu._run_usage_monitor(message)

        monitoring_times = [
            timestamp
            for timestamp, text in zip(edit_times, rendered)
            if "Monitoring system usage" in text
        ]
        self.assertEqual(len(monitoring_times), 3)
        self.assertAlmostEqual(monitoring_times[0], 0.2)
        self.assertAlmostEqual(monitoring_times[1], 1.2)
        self.assertAlmostEqual(monitoring_times[2], 2.2)
        self.assertAlmostEqual(sleep_delays[0], 0.8)
        self.assertAlmostEqual(sleep_delays[1], 0.8)
        self.assertEqual(rendered[-1], "🛑 Stopped monitoring system usage.")


if __name__ == "__main__":
    unittest.main()
