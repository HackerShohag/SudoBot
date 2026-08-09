"""Track cancellable Telegram work by chat for the global /stop command."""

import asyncio
from dataclasses import dataclass


_CHAT_TASKS = {}


@dataclass
class _TrackedTask:
    label: str
    cancel_requested: bool = False


def chat_task_key(update):
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        message = getattr(update, "message", None)
        chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return ("chat", chat_id)

    user = getattr(update, "effective_user", None)
    if user is None:
        message = getattr(update, "message", None)
        user = getattr(message, "from_user", None)
    user_id = getattr(user, "id", None)
    return ("user", user_id if user_id is not None else id(update))


def _forget_task(key, task):
    tasks = _CHAT_TASKS.get(key)
    if tasks is None:
        return
    tasks.pop(task, None)
    if not tasks:
        _CHAT_TASKS.pop(key, None)


def register_task(update, task, label):
    """Register an asyncio task and remove it automatically on completion."""
    if task is None:
        return None
    key = chat_task_key(update)
    tasks = _CHAT_TASKS.setdefault(key, {})
    if task in tasks:
        tasks[task].label = str(label)
        return task
    tasks[task] = _TrackedTask(str(label))
    task.add_done_callback(lambda completed: _forget_task(key, completed))
    return task


def register_current_task(update, label):
    return register_task(update, asyncio.current_task(), label)


def unregister_task(update, task):
    if task is not None:
        _forget_task(chat_task_key(update), task)


def cancel_chat_tasks(update, *, exclude=None):
    """Cancel and return all currently running tracked tasks in one chat."""
    key = chat_task_key(update)
    cancelled = []
    excluded = (
        set(exclude)
        if isinstance(exclude, (set, frozenset, list, tuple))
        else {exclude}
    )
    for task, entry in list(_CHAT_TASKS.get(key, {}).items()):
        if task in excluded:
            continue
        if task.done():
            _forget_task(key, task)
            continue
        # Keep the entry until its done callback fires, but mark it first so a
        # repeated /stop cannot inject a second CancelledError during cleanup.
        if entry.cancel_requested:
            continue
        entry.cancel_requested = True
        if task.cancel():
            cancelled.append((task, entry.label))
        else:
            _forget_task(key, task)
    return cancelled


def tracked_tasks(update):
    """Return a snapshot for status reporting and tests."""
    return {
        task: entry.label
        for task, entry in _CHAT_TASKS.get(chat_task_key(update), {}).items()
        if not task.done()
    }


def has_stopping_tasks(update):
    """Return whether cancellation cleanup is still running in this chat."""
    return any(
        entry.cancel_requested and not task.done()
        for task, entry in _CHAT_TASKS.get(chat_task_key(update), {}).items()
    )
