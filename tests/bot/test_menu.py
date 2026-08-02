import asyncio
import io
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import NetworkError, RetryAfter

from bot import menu


def make_update(status=None):
    status = status or SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="testuser"),
        reply_text=AsyncMock(return_value=status),
    )
    return SimpleNamespace(message=message), status


class TestMenu(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        menu._ACTIVE_MONITORS.clear()

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
            "⏳ Collecting machine specifications..."
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
        task = SimpleNamespace(done=Mock(return_value=False), add_done_callback=Mock())
        create_task = Mock(return_value=task)
        context = SimpleNamespace(application=SimpleNamespace(create_task=create_task))

        with patch("bot.menu.is_user_authorized", return_value=True):
            await menu.monitor_system_usage(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "🟢 Starting system monitor — gathering the first reading..."
        )
        context.application.create_task.assert_called_once()
        monitor_coroutine = context.application.create_task.call_args.args[0]
        self.assertTrue(asyncio.iscoroutine(monitor_coroutine))
        task.add_done_callback.assert_called_once()
        monitor_coroutine.close()

    async def test_monitor_rejects_a_second_task_for_same_chat_and_user(self):
        update, _ = make_update()
        task = SimpleNamespace(done=Mock(return_value=False), add_done_callback=Mock())
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


if __name__ == "__main__":
    unittest.main()
