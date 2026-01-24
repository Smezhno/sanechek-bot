"""Scheduler for automatic reminders and summaries."""
import logging
from datetime import datetime, time, timedelta

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application
from sqlalchemy import select

from config import settings
from database import get_session, Reminder, Task, User, Chat, ReminderStatus, TaskStatus
from handlers.reminders import send_reminder
from handlers.summary import send_daily_summaries
from utils.formatters import format_date

logger = logging.getLogger(__name__)


def _build_task_reminder_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Build keyboard with task actions for reminder messages."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Закрыть", callback_data=f"task:close:{task_id}"),
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"task:edit:{task_id}"),
        ]
    ])


def setup_scheduler(app: Application) -> None:
    """Setup scheduled jobs for the bot."""
    tz = pytz.timezone(settings.timezone)
    
    # Parse summary time
    hour, minute = map(int, settings.summary_time.split(":"))
    summary_time = time(hour=hour, minute=minute, tzinfo=tz)
    
    # Daily summary job
    app.job_queue.run_daily(
        send_daily_summaries_job,
        time=summary_time,
        name="daily_summary"
    )
    
    # Reminder check job (runs every minute)
    app.job_queue.run_repeating(
        check_reminders_job,
        interval=60,
        first=10,  # Start after 10 seconds
        name="check_reminders"
    )
    
    # Task reminder job (runs every hour)
    app.job_queue.run_repeating(
        check_task_deadlines_job,
        interval=3600,
        first=60,  # Start after 1 minute
        name="check_task_deadlines"
    )
    
    # Overdue task reminder (runs daily at 12:00)
    app.job_queue.run_daily(
        send_overdue_reminders_job,
        time=summary_time,
        name="overdue_reminders"
    )


async def send_daily_summaries_job(context) -> None:
    """Job to send daily summaries."""
    await send_daily_summaries(context)


async def check_reminders_job(context) -> None:
    """Job to check and send due reminders."""
    now = datetime.utcnow()
    
    async with get_session() as session:
        # Get all pending reminders that are due
        result = await session.execute(
            select(Reminder)
            .where(
                Reminder.status == ReminderStatus.PENDING,
                Reminder.remind_at <= now
            )
        )
        reminders = result.scalars().all()
        
        for reminder in reminders:
            await send_reminder(context, reminder)


async def check_task_deadlines_job(context) -> None:
    """Job to send reminders for tasks approaching deadline."""
    now = datetime.utcnow()
    reminder_threshold = now + timedelta(hours=settings.task_reminder_hours_before)

    async with get_session() as session:
        # Get tasks with deadline approaching (only tasks that have deadline)
        result = await session.execute(
            select(Task)
            .where(
                Task.status == TaskStatus.OPEN,
                Task.reminder_sent == False,
                Task.deadline.isnot(None),
                Task.deadline <= reminder_threshold,
                Task.deadline > now  # Not yet overdue
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            # Get assignee
            result = await session.execute(
                select(User).where(User.id == task.assignee_id)
            )
            assignee = result.scalar_one_or_none()

            if not assignee:
                continue

            # Get chat
            result = await session.execute(
                select(Chat).where(Chat.id == task.chat_id)
            )
            chat = result.scalar_one_or_none()

            if not chat:
                continue

            # Calculate time until deadline
            time_left = task.deadline - now
            hours_left = int(time_left.total_seconds() / 3600)

            deadline_str = format_date(task.deadline, include_time=True)

            text = (
                f"⏰ Напоминание!\n\n"
                f"Задача: {task.text}\n"
                f"Чат: {chat.title}\n"
                f"Дедлайн: через {hours_left} ч. ({deadline_str})"
            )

            keyboard = _build_task_reminder_keyboard(task.id)

            try:
                await context.bot.send_message(
                    chat_id=assignee.id,
                    text=text,
                    reply_markup=keyboard
                )
                task.reminder_sent = True
            except Exception as e:
                # User might have blocked the bot
                logger.debug("Failed to send task reminder to user %s: %s", assignee.id, e)


async def send_overdue_reminders_job(context) -> None:
    """Job to send reminders about overdue tasks."""
    now = datetime.utcnow()

    async with get_session() as session:
        # Get overdue tasks (only tasks that have deadline)
        result = await session.execute(
            select(Task)
            .where(
                Task.status == TaskStatus.OPEN,
                Task.deadline.isnot(None),
                Task.deadline < now
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            # Get assignee
            result = await session.execute(
                select(User).where(User.id == task.assignee_id)
            )
            assignee = result.scalar_one_or_none()

            if not assignee:
                continue

            # Get chat
            result = await session.execute(
                select(Chat).where(Chat.id == task.chat_id)
            )
            chat = result.scalar_one_or_none()

            if not chat:
                continue

            # Calculate overdue time
            overdue_time = now - task.deadline
            days_overdue = overdue_time.days

            if days_overdue == 0:
                overdue_str = "сегодня"
            elif days_overdue == 1:
                overdue_str = "на 1 день"
            else:
                overdue_str = f"на {days_overdue} дней"

            text = (
                f"⚠️ Просроченная задача!\n\n"
                f"Задача: {task.text}\n"
                f"Чат: {chat.title}\n"
                f"Дедлайн: просрочен {overdue_str}"
            )

            keyboard = _build_task_reminder_keyboard(task.id)

            try:
                await context.bot.send_message(
                    chat_id=assignee.id,
                    text=text,
                    reply_markup=keyboard
                )
            except Exception as e:
                # User might have blocked the bot
                logger.debug("Failed to send overdue reminder to user %s: %s", assignee.id, e)

