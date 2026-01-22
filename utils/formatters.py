"""Text formatting utilities."""
from datetime import datetime
from typing import Optional
import pytz

from config import settings


def get_timezone():
    """Get configured timezone."""
    return pytz.timezone(settings.timezone)


def format_date(dt: datetime, include_time: bool = False) -> str:
    """Format datetime for display."""
    tz = get_timezone()
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    local_dt = dt.astimezone(tz)
    
    if include_time:
        return local_dt.strftime("%d.%m.%Y в %H:%M")
    return local_dt.strftime("%d.%m.%Y")


def format_relative_date(dt: datetime) -> str:
    """Format datetime relative to now (e.g., 'через 4 часа')."""
    tz = get_timezone()
    now = datetime.now(tz)
    
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    local_dt = dt.astimezone(tz)
    
    diff = local_dt - now
    total_seconds = diff.total_seconds()
    
    if total_seconds < 0:
        # Past
        days = abs(total_seconds) // 86400
        if days == 0:
            return "сегодня"
        elif days == 1:
            return "вчера"
        elif days < 7:
            return f"{int(days)} дн. назад"
        else:
            return format_date(dt)
    else:
        # Future
        hours = total_seconds / 3600
        if hours < 1:
            minutes = total_seconds / 60
            return f"через {int(minutes)} мин."
        elif hours < 24:
            return f"через {int(hours)} ч."
        elif hours < 48:
            return "завтра"
        else:
            days = hours / 24
            return f"через {int(days)} дн."


def format_task(task, include_chat: bool = False, include_author: bool = False) -> str:
    """Format task for display."""
    lines = []
    
    # Task text
    text = task.text
    if len(text) > 100:
        text = text[:100] + "..."
    
    lines.append(f"📌 {text}")
    
    if include_chat and task.chat:
        lines.append(f"Чат: {task.chat.title}")
    
    if include_author and task.author:
        lines.append(f"Автор: {task.author.display_name}")
    
    if task.assignee:
        lines.append(f"Исполнитель: {task.assignee.display_name}")
    
    # Deadline
    deadline_str = format_date(task.deadline)
    if task.is_overdue:
        lines.append(f"Дедлайн: {deadline_str} ⚠️ просрочена")
    else:
        lines.append(f"Дедлайн: {deadline_str}")
    
    return "\n".join(lines)


def format_task_short(task) -> str:
    """Format task in short form for lists."""
    text = task.text
    if len(text) > 50:
        text = text[:50] + "..."
    
    deadline_str = format_date(task.deadline)
    overdue_mark = " ⚠️ просрочена" if task.is_overdue else ""
    
    return f"{text}\n   Исполнитель: {task.assignee.display_name} | Дедлайн: {deadline_str}{overdue_mark}"


def format_expense(expense) -> str:
    """Format expense for display."""
    amount_str = format_amount(expense.amount)
    return f"💰 {amount_str} — {expense.description} (категория: {expense.category})"


def format_amount(amount: float) -> str:
    """Format monetary amount."""
    # Format with thousands separator
    if amount == int(amount):
        formatted = f"{int(amount):,}".replace(",", " ")
    else:
        formatted = f"{amount:,.2f}".replace(",", " ")
    return f"{formatted} ₽"


def format_reminder(reminder, include_chat: bool = False) -> str:
    """Format reminder for display."""
    lines = []
    
    # Reminder text
    text = reminder.text
    if len(text) > 100:
        text = text[:100] + "..."
    
    lines.append(f'🔔 "{text}"')
    
    if include_chat and reminder.chat:
        lines.append(f"Чат: {reminder.chat.title}")
    
    # Time
    time_str = format_date(reminder.remind_at, include_time=True)
    lines.append(f"Когда: {time_str}")
    
    # Recipient and author
    if reminder.recipient:
        lines.append(f"Кому: {reminder.recipient.display_name}")
    if reminder.author and reminder.author.id != reminder.recipient.id:
        lines.append(f"Создал: {reminder.author.display_name}")
    
    return "\n".join(lines)


def format_reminder_short(reminder) -> str:
    """Format reminder in short form for lists."""
    text = reminder.text
    if len(text) > 40:
        text = text[:40] + "..."
    
    time_str = format_date(reminder.remind_at, include_time=True)
    
    return f'"{text}" — {time_str}'


def truncate_summary(text: str, max_length: int = 4096) -> str:
    """Truncate summary text to fit Telegram message limit."""
    if len(text) <= max_length:
        return text
    
    suffix = "\n\n(саммари сокращено)"
    return text[:max_length - len(suffix)] + suffix

