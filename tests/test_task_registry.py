import asyncio
import unittest
from types import SimpleNamespace

from bot import task_registry


def task_update(chat_id, user_id=1):
    user = SimpleNamespace(id=user_id, username=f"user-{user_id}")
    message = SimpleNamespace(chat_id=chat_id, from_user=user)
    return SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=user,
    )


class TaskRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        task_registry._CHAT_TASKS.clear()

    async def test_cancel_chat_tasks_cancels_every_task_only_in_that_chat(self):
        update_a = task_update(100)
        update_b = task_update(200)
        blocker = asyncio.Event()

        task_a1 = asyncio.create_task(blocker.wait())
        task_a2 = asyncio.create_task(blocker.wait())
        task_b = asyncio.create_task(blocker.wait())
        task_registry.register_task(update_a, task_a1, "PDF processing")
        task_registry.register_task(update_a, task_a2, "System monitor")
        task_registry.register_task(update_b, task_b, "System usage")

        cancelled = task_registry.cancel_chat_tasks(update_a)
        await asyncio.sleep(0)

        self.assertEqual(
            {label for _task, label in cancelled},
            {"PDF processing", "System monitor"},
        )
        self.assertTrue(task_a1.cancelled())
        self.assertTrue(task_a2.cancelled())
        self.assertFalse(task_b.done())
        self.assertEqual(task_registry.tracked_tasks(update_a), {})
        self.assertEqual(
            task_registry.tracked_tasks(update_b),
            {task_b: "System usage"},
        )

        task_b.cancel()
        await asyncio.gather(task_b, return_exceptions=True)

    async def test_repeated_cancel_does_not_interrupt_task_cleanup(self):
        update = task_update(100)
        running = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()

        async def worker():
            running.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                raise

        task = asyncio.create_task(worker())
        task_registry.register_task(update, task, "PDF processing")
        await running.wait()

        first = task_registry.cancel_chat_tasks(update)
        await cleanup_started.wait()
        second = task_registry.cancel_chat_tasks(update)

        self.assertEqual(first, [(task, "PDF processing")])
        self.assertEqual(second, [])
        self.assertEqual(task.cancelling(), 1)
        self.assertFalse(task.done())
        self.assertTrue(task_registry.has_stopping_tasks(update))

        cleanup_release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertFalse(task_registry.has_stopping_tasks(update))

    async def test_completed_task_is_removed_without_affecting_other_chat(self):
        update_a = task_update(100)
        update_b = task_update(200)
        task_a = asyncio.create_task(asyncio.sleep(0))
        task_b = asyncio.create_task(asyncio.Event().wait())
        task_registry.register_task(update_a, task_a, "Quick status")
        task_registry.register_task(update_b, task_b, "Long status")

        await task_a
        await asyncio.sleep(0)

        self.assertEqual(task_registry.tracked_tasks(update_a), {})
        self.assertEqual(
            task_registry.tracked_tasks(update_b),
            {task_b: "Long status"},
        )

        task_b.cancel()
        await asyncio.gather(task_b, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
