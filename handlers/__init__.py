"""Handlers package."""
from handlers.base import setup_handlers
from handlers.start import start_handler, help_handler, cancel_handler
from handlers.tasks import (
    task_handler, tasks_handler, mytasks_handler,
    done_handler, edit_handler
)
from handlers.expenses import cost_handler
from handlers.reminders import remind_handler, reminders_handler
from handlers.summary import summary_handler, subscribe_handler
from handlers.admin import setadmin_handler, removeadmin_handler, admins_handler

__all__ = [
    "setup_handlers",
    "start_handler", "help_handler", "cancel_handler",
    "task_handler", "tasks_handler", "mytasks_handler",
    "done_handler", "edit_handler",
    "cost_handler",
    "remind_handler", "reminders_handler",
    "summary_handler", "subscribe_handler",
    "setadmin_handler", "removeadmin_handler", "admins_handler",
]

