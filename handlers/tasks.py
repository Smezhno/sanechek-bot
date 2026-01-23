"""Task management handlers."""
import logging
import re
from datetime import datetime, date, timedelta
from typing import Optional, TypedDict

from dateutil.relativedelta import relativedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from sqlalchemy import select, and_

from database import get_session, Task, User, Chat, ChatMember, TaskStatus
from database.models import RecurrenceType
from handlers.base import States
from llm.client import ask_llm
from utils.date_parser import parse_deadline, DateParseError
from utils.formatters import format_task, format_task_short, format_date
from utils.permissions import (
    get_or_create_user, is_admin, can_close_task, can_edit_task,
    is_user_in_chat
)
from config import settings


logger = logging.getLogger(__name__)

# Constants
MAX_USER_BUTTONS = 5

# Message constants
MSG_GROUP_ONLY = "Эта команда работает только в групповых чатах"
MSG_REPLY_TO_TASK = "Ответь на сообщение с задачей"
MSG_NOT_A_TASK = "Это не задача. Ответь на сообщение с задачей"
MSG_TASK_ALREADY_CLOSED = "Эта задача уже закрыта"
MSG_CANT_CLOSE = "Закрыть задачу может только исполнитель, автор или админ"
MSG_CANT_EDIT = "Редактировать задачу может только автор или админ"
MSG_ASSIGNEE_NOT_FOUND = "Не удалось найти исполнителя. Попробуй создать задачу заново."
MSG_USER_NOT_FOUND = "Пользователь не найден"
MSG_ASK_ASSIGNEE = "Кто исполнитель? Укажи ответным сообщением @username"
MSG_ASK_DEADLINE = "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
MSG_NO_ACTIVE_TASKS = "📋 Активных задач нет"
MSG_NO_YOUR_TASKS = "📋 У тебя нет активных задач"
MSG_NO_YOUR_TASKS_IN_CHAT = "📋 У тебя нет активных задач в этом чате"
MSG_DM_EDIT_HINT = "В ЛС используй кнопки под задачей для редактирования"


class ParsedTask(TypedDict, total=False):
    """Structure for parsed task data."""
    task: str
    assignee_id: Optional[int]
    assignee_username: Optional[str]
    assignee_name: Optional[str]
    deadline: Optional[datetime]
    recurrence: Optional[RecurrenceType]
    is_self: bool
    is_complete: bool
    multiple_candidates: Optional[list]


# Recurrence patterns
RECURRENCE_PATTERNS = {
    RecurrenceType.DAILY: [
        "каждый день", "ежедневно", "каждое утро", "по утрам",
        "каждый вечер", "по вечерам", "перед сном", "на ночь",
        "утром", "вечером"
    ],
    RecurrenceType.WEEKDAYS: [
        "по будням", "пн-пт", "будни", "в рабочие дни", "по рабочим дням"
    ],
    RecurrenceType.WEEKLY: [
        "каждый понедельник", "каждый вторник", "каждую среду",
        "каждый четверг", "каждую пятницу", "каждую субботу",
        "каждое воскресенье", "еженедельно", "раз в неделю",
        "по понедельникам", "по вторникам", "по средам",
        "по четвергам", "по пятницам", "по субботам", "по воскресеньям",
        "каждую неделю"
    ],
    RecurrenceType.MONTHLY: [
        "каждый месяц", "ежемесячно", "раз в месяц",
        "в начале месяца", "в конце месяца", "1 числа"
    ],
}

DAY_MAP = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
    "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6,
}

SELF_KEYWORDS = ["я", "мне", "себе", "сам", "сама", "себя"]
SELF_PHRASES = ["мне ", "мне,", "себе ", "я должен", "я должна", "мне нужно", "мне надо"]


def _build_recurrence_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for recurrence selection."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Каждый день", callback_data="recurrence:daily")],
        [InlineKeyboardButton("📅 Пн-Пт", callback_data="recurrence:weekdays")],
        [InlineKeyboardButton("📆 Каждую неделю", callback_data="recurrence:weekly")],
        [InlineKeyboardButton("🗓️ Каждый месяц", callback_data="recurrence:monthly")],
        [InlineKeyboardButton("➡️ Без повтора", callback_data="recurrence:none")],
    ])


def _get_recurrence_label(recurrence: str) -> str:
    """Get human-readable recurrence label."""
    labels = {
        "none": "без повтора",
        "daily": "каждый день",
        "weekdays": "Пн-Пт",
        "weekly": "каждую неделю",
        "monthly": "каждый месяц",
    }
    return labels.get(recurrence, recurrence)


def _recurrence_str_to_enum(recurrence_str: str) -> RecurrenceType:
    """Convert recurrence string to enum."""
    mapping = {
        "none": RecurrenceType.NONE,
        "daily": RecurrenceType.DAILY,
        "weekdays": RecurrenceType.WEEKDAYS,
        "weekly": RecurrenceType.WEEKLY,
        "monthly": RecurrenceType.MONTHLY,
    }
    return mapping.get(recurrence_str, RecurrenceType.NONE)


def _build_task_action_keyboard(task_id: int, include_edit: bool = True) -> InlineKeyboardMarkup:
    """Build keyboard with task action buttons."""
    buttons = [InlineKeyboardButton("✅ Закрыть", callback_data=f"task:close:{task_id}")]
    if include_edit:
        buttons.append(InlineKeyboardButton("✏️ Редактировать", callback_data=f"task:edit:{task_id}"))
    return InlineKeyboardMarkup([buttons])


def _detect_time_of_day(text: str) -> int:
    """Detect time of day from text, return default hour."""
    if "утр" in text:
        return 9
    elif "вечер" in text:
        return 19
    elif "сном" in text or "ночь" in text:
        return 22
    return 12


def _parse_time_from_text(text: str) -> tuple[int, int]:
    """Parse specific time from text like 'в 15:00' or 'в 12 часов'."""
    time_match = re.search(r"в\s*(\d{1,2})(?:[:\s](\d{2}))?\s*(?:час|:)?", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
        return hour, minute
    return None, None


def _detect_recurrence(text: str) -> Optional[RecurrenceType]:
    """Detect recurrence type from text."""
    text_lower = text.lower()
    for recurrence, patterns in RECURRENCE_PATTERNS.items():
        for pattern in patterns:
            if pattern in text_lower:
                return recurrence
    return None


def _calculate_next_weekday(target_weekday: int, base_date: date = None) -> date:
    """Calculate next occurrence of a weekday."""
    if base_date is None:
        base_date = date.today()
    days_ahead = target_weekday - base_date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return base_date + timedelta(days=days_ahead)


def _is_self_assignment(text: str) -> bool:
    """Check if text indicates self-assignment."""
    text_lower = text.lower()
    for phrase in SELF_PHRASES:
        if phrase in text_lower:
            return True
    return False


async def _get_chat_members(session, chat_id: int) -> list[User]:
    """Get all members of a chat."""
    result = await session.execute(
        select(User).join(ChatMember).where(ChatMember.chat_id == chat_id)
    )
    return result.scalars().all()


async def _find_user_by_username(session, username: str) -> Optional[User]:
    """Find user by username."""
    result = await session.execute(
        select(User).where(User.username == username)
    )
    return result.scalar_one_or_none()


async def _find_user_by_name_fuzzy(members: list[User], name: str) -> list[User]:
    """Find users matching name (fuzzy match)."""
    text_lower = name.lower().strip()
    matching = []

    for m in members:
        first = (m.first_name or "").lower()
        last = (m.last_name or "").lower()
        full = f"{first} {last}".strip()

        if (text_lower == first or
            text_lower == last or
            text_lower == full or
            text_lower in first or
                first.startswith(text_lower)):
            matching.append(m)

    return matching


def _build_user_selection_buttons(users: list[User], callback_prefix: str) -> list[list[InlineKeyboardButton]]:
    """Build inline buttons for user selection."""
    buttons = []
    for user in users[:MAX_USER_BUTTONS]:
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        buttons.append([
            InlineKeyboardButton(
                f"{name} (@{user.username})",
                callback_data=f"{callback_prefix}:{user.id}:{user.username}"
            )
        ])
    return buttons


# --- LLM Parsing ---

async def _llm_parse_task(text: str, members_list: str) -> dict:
    """Use LLM to parse task components."""
    prompt = f'''Распарси задачу и извлеки компоненты.

Текст: "{text}"
Участники чата: {members_list}

Определи:
1. ЗАДАЧА - что нужно сделать (очисти от служебных слов)
2. ИСПОЛНИТЕЛЬ - "я" если мне/себе/я должен, или @username участника, или "не указан"
3. ДЕДЛАЙН - конкретная дата/время или "не указан"
4. ПОВТОР - none/daily/weekdays/weekly/monthly или "не указан"

Примеры повтора:
- "каждый день" → daily
- "каждый понедельник", "по понедельникам", "еженедельно" → weekly
- "каждый месяц", "ежемесячно" → monthly
- "по будням", "пн-пт" → weekdays

Ответь СТРОГО в формате:
ЗАДАЧА: <текст>
ИСПОЛНИТЕЛЬ: <я/@username/не указан>
ДЕДЛАЙН: <дата или не указан>
ПОВТОР: <none/daily/weekdays/weekly/monthly>'''

    response = await ask_llm(
        question=prompt,
        system_prompt="Ты парсер задач. Отвечай строго в указанном формате, без пояснений.",
        max_tokens=150,
        temperature=0.1
    )
    return response


async def _llm_find_assignee(text: str, members_list: str) -> str:
    """Use LLM to find assignee from text."""
    prompt = f"""Из текста задачи определи исполнителя и саму задачу.

Участники чата: {members_list}

Текст: "{text}"

Ответь строго в формате:
ИСПОЛНИТЕЛЬ: @username (или "не указан", или "несколько:@user1,@user2" если подходят несколько)
ЗАДАЧА: текст задачи без имени исполнителя

Если имя похоже на одного из участников (Вася=Василий, Саша=Александр и т.д.), укажи его @username.
Если подходят несколько участников — перечисли всех через запятую."""

    return await ask_llm(
        question=prompt,
        system_prompt="Ты парсер задач. Отвечай строго в указанном формате.",
        max_tokens=150,
        temperature=0.1
    )


async def _llm_match_name(name: str, members_list: str) -> str:
    """Use LLM to match name to username."""
    prompt = f"""Кто из участников соответствует имени "{name}"?

Участники: {members_list}

Учитывай уменьшительные имена: Витя=Виктор, Саша=Александр, Давид=David, Дима=Дмитрий и т.д.

Ответь ТОЛЬКО @username одного человека или "не найден"."""

    return await ask_llm(
        question=prompt,
        system_prompt="Ты определяешь пользователя по имени. Отвечай только @username.",
        max_tokens=50,
        temperature=0.1
    )


def _parse_llm_task_response(response: str, members: list[User]) -> dict:
    """Parse LLM response for task components."""
    result = {
        "task": None,
        "is_self": False,
        "assignee_id": None,
        "assignee_username": None,
        "deadline": None,
        "recurrence": None,
    }

    recurrence_map = {
        "daily": RecurrenceType.DAILY,
        "weekdays": RecurrenceType.WEEKDAYS,
        "weekly": RecurrenceType.WEEKLY,
        "monthly": RecurrenceType.MONTHLY,
        "none": RecurrenceType.NONE,
    }

    for line in response.split("\n"):
        line = line.strip()

        if line.upper().startswith("ЗАДАЧА:"):
            task_text = line.split(":", 1)[1].strip()
            if task_text and task_text.lower() != "не указан":
                result["task"] = task_text

        elif line.upper().startswith("ИСПОЛНИТЕЛЬ:"):
            assignee = line.split(":", 1)[1].strip().lower()
            if assignee == "я":
                result["is_self"] = True
            elif "@" in assignee:
                username_match = re.search(r"@(\w+)", assignee)
                if username_match:
                    username = username_match.group(1)
                    for m in members:
                        if m.username and m.username.lower() == username.lower():
                            result["assignee_id"] = m.id
                            result["assignee_username"] = m.username
                            break

        elif line.upper().startswith("ДЕДЛАЙН:"):
            deadline_text = line.split(":", 1)[1].strip()
            if deadline_text and deadline_text.lower() != "не указан":
                try:
                    result["deadline"] = parse_deadline(deadline_text)
                except (DateParseError, Exception):
                    pass

        elif line.upper().startswith("ПОВТОР:"):
            recurrence = line.split(":", 1)[1].strip().lower()
            if recurrence in recurrence_map:
                result["recurrence"] = recurrence_map[recurrence]

    return result


async def _smart_parse_task(text: str, chat_id: int, author_id: int = None) -> ParsedTask:
    """Parse task text using LLM to extract ALL task components."""
    result: ParsedTask = {
        "task": text,
        "assignee_id": None,
        "assignee_username": None,
        "assignee_name": None,
        "deadline": None,
        "recurrence": None,
        "is_self": False,
        "is_complete": False,
    }

    members = []

    # Use LLM if available
    if settings.yandex_gpt_api_key or settings.openai_api_key:
        try:
            async with get_session() as session:
                members = await _get_chat_members(session, chat_id)

                members_list = ", ".join([
                    f"{m.first_name or ''} (@{m.username})"
                    for m in members if m.username
                ]) or "неизвестны"

            llm_response = await _llm_parse_task(text, members_list)
            parsed = _parse_llm_task_response(llm_response, members)

            if parsed["task"]:
                result["task"] = parsed["task"]
            if parsed["is_self"]:
                result["is_self"] = True
            if parsed["assignee_id"]:
                result["assignee_id"] = parsed["assignee_id"]
                result["assignee_username"] = parsed["assignee_username"]
            if parsed["deadline"]:
                result["deadline"] = parsed["deadline"]
            if parsed["recurrence"]:
                result["recurrence"] = parsed["recurrence"]

        except Exception as e:
            logger.debug(f"LLM parsing failed: {e}")

    # Fallback: Check for self-assignment
    if not result["is_self"] and not result["assignee_id"]:
        if _is_self_assignment(text):
            result["is_self"] = True
            for phrase in SELF_PHRASES:
                if phrase.strip() in text.lower():
                    result["task"] = re.sub(rf"(?i){phrase.strip()}\s*", "", text).strip()
                    break

    # Fallback: Check for recurrence patterns
    if not result["recurrence"]:
        result = _parse_recurrence_fallback(text, result)

    # Fallback: Check for @username
    if not result["assignee_id"] and not result["is_self"]:
        result = await _parse_username_fallback(text, result)

    # Fallback: Try LLM for name matching
    if not result["assignee_id"] and not result["is_self"]:
        result = await _llm_find_assignee_fallback(text, chat_id, members, result)

    # Fallback: Parse deadline patterns
    if not result["deadline"]:
        result = _parse_deadline_fallback(text, result)

    # Heuristic: Recurring tasks without assignee are self-tasks
    if result["recurrence"] and not result["is_self"] and not result["assignee_id"]:
        result["is_self"] = True

    # Clean up task text
    result["task"] = " ".join(result["task"].split())

    return result


def _parse_recurrence_fallback(text: str, result: ParsedTask) -> ParsedTask:
    """Parse recurrence patterns from text (fallback)."""
    text_lower = text.lower()

    # Time patterns with default hours
    time_patterns = {
        "по утрам": (RecurrenceType.DAILY, 9),
        "каждое утро": (RecurrenceType.DAILY, 9),
        "утром каждый день": (RecurrenceType.DAILY, 9),
        "по вечерам": (RecurrenceType.DAILY, 19),
        "каждый вечер": (RecurrenceType.DAILY, 19),
        "вечером каждый день": (RecurrenceType.DAILY, 19),
        "перед сном": (RecurrenceType.DAILY, 22),
        "на ночь": (RecurrenceType.DAILY, 22),
    }

    for pattern, (recurrence, default_hour) in time_patterns.items():
        if pattern in text_lower:
            result["recurrence"] = recurrence
            result["task"] = re.sub(rf"(?i){pattern}", "", result["task"]).strip()

            hour, minute = _parse_time_from_text(text_lower)
            if hour is None:
                hour = default_hour
                minute = 0

            tomorrow = date.today() + timedelta(days=1)
            result["deadline"] = datetime.combine(
                tomorrow, datetime.min.time().replace(hour=hour, minute=minute)
            )
            return result

    # Regular recurrence patterns
    detected = _detect_recurrence(text)
    if detected:
        result["recurrence"] = detected

        # Remove pattern from task text
        for pattern in RECURRENCE_PATTERNS.get(detected, []):
            if pattern in text_lower:
                result["task"] = re.sub(rf"(?i){pattern}", "", result["task"]).strip()
                break

        # Calculate deadline for weekly tasks
        if detected == RecurrenceType.WEEKLY:
            for day_name, weekday in DAY_MAP.items():
                if day_name in text_lower:
                    next_date = _calculate_next_weekday(weekday)
                    result["deadline"] = datetime.combine(
                        next_date, datetime.min.time().replace(hour=12)
                    )
                    break

        # Default deadline
        if not result["deadline"]:
            tomorrow = date.today() + timedelta(days=1)
            result["deadline"] = datetime.combine(
                tomorrow, datetime.min.time().replace(hour=12)
            )

    return result


async def _parse_username_fallback(text: str, result: ParsedTask) -> ParsedTask:
    """Parse @username from text (fallback)."""
    username_match = re.search(r"@(\w+)", text)
    if username_match:
        username = username_match.group(1)
        async with get_session() as session:
            user = await _find_user_by_username(session, username)
            if user:
                result["assignee_id"] = user.id
                result["assignee_username"] = username
                if result["task"] == text:
                    result["task"] = text.replace(f"@{username}", "").strip()
    return result


async def _llm_find_assignee_fallback(
    text: str,
    chat_id: int,
    members: list[User],
    result: ParsedTask
) -> ParsedTask:
    """Use LLM to find assignee from name (fallback)."""
    if not (settings.yandex_gpt_api_key or settings.openai_api_key):
        return result

    if not members:
        async with get_session() as session:
            members = await _get_chat_members(session, chat_id)

    if not members:
        return result

    try:
        members_list = ", ".join([
            f"{m.first_name or ''} {m.last_name or ''} (@{m.username})"
            for m in members if m.username
        ])

        response = await _llm_find_assignee(text, members_list)

        for line in response.split("\n"):
            if "ИСПОЛНИТЕЛЬ:" in line.upper():
                if "несколько" in line.lower() or "," in line:
                    usernames = re.findall(r"@(\w+)", line)
                    if len(usernames) > 1:
                        result["multiple_candidates"] = []
                        for username in usernames:
                            for m in members:
                                if m.username and m.username.lower() == username.lower():
                                    result["multiple_candidates"].append({
                                        "id": m.id,
                                        "username": m.username,
                                        "name": f"{m.first_name or ''} {m.last_name or ''}".strip()
                                    })
                                    break
                else:
                    match = re.search(r"@(\w+)", line)
                    if match:
                        username = match.group(1)
                        for m in members:
                            if m.username and m.username.lower() == username.lower():
                                result["assignee_id"] = m.id
                                result["assignee_username"] = m.username
                                break
            elif "ЗАДАЧА:" in line.upper():
                task = line.split(":", 1)[1].strip() if ":" in line else ""
                if task:
                    result["task"] = task

    except Exception as e:
        logger.debug(f"LLM assignee lookup failed: {e}")

    return result


def _parse_deadline_fallback(text: str, result: ParsedTask) -> ParsedTask:
    """Parse deadline patterns from text (fallback)."""
    deadline_patterns = [
        r"(завтра|послезавтра|сегодня)",
        r"(через\s+\d+\s+(?:час|часа|часов|минут|минуты|дн|день|дней))",
    ]

    for pattern in deadline_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                result["deadline"] = parse_deadline(match.group(1))
                result["task"] = result["task"].replace(match.group(1), "").strip()
            except DateParseError:
                pass
            break

    return result


# --- Main Handlers ---

async def task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /task command - create a new task."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(MSG_GROUP_ONLY)
        return ConversationHandler.END

    user = update.effective_user
    chat = update.effective_chat
    args = " ".join(context.args) if context.args else ""

    async with get_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == chat.id))
        db_chat = result.scalar_one_or_none()
        if not db_chat:
            db_chat = Chat(id=chat.id, title=chat.title, is_active=True)
            session.add(db_chat)

        await get_or_create_user(
            session, user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )

    context.user_data["in_conversation"] = True
    context.user_data["task_chat_id"] = chat.id
    context.user_data["task_author_id"] = user.id
    context.user_data["task_command_message_id"] = update.message.message_id

    if not args:
        await update.message.reply_text("Что нужно сделать? Укажи ответным сообщением")
        return States.TASK_TEXT

    parsed = await _smart_parse_task(args, chat.id, user.id)
    context.user_data["task_text"] = parsed["task"][:settings.max_task_length]

    # Handle self-assignment
    if parsed.get("is_self"):
        async with get_session() as session:
            result = await session.execute(select(User).where(User.id == user.id))
            author = result.scalar_one_or_none()
            if author:
                parsed["assignee_id"] = user.id
                parsed["assignee_username"] = author.username

    return await _route_parsed_task(update, context, parsed)


async def _route_parsed_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed: ParsedTask
) -> int:
    """Route to appropriate next step based on parsed task data."""
    # Full data - create immediately
    if parsed.get("assignee_id") and parsed.get("deadline") and parsed.get("recurrence"):
        context.user_data["task_assignee_id"] = parsed["assignee_id"]
        context.user_data["task_assignee_username"] = parsed["assignee_username"]
        context.user_data["task_deadline"] = parsed["deadline"]
        context.user_data["task_recurrence"] = parsed["recurrence"].value
        return await _create_task(update, context)

    # Has assignee and deadline - ask about recurrence
    if parsed.get("assignee_id") and parsed.get("deadline"):
        context.user_data["task_assignee_id"] = parsed["assignee_id"]
        context.user_data["task_assignee_username"] = parsed["assignee_username"]
        context.user_data["task_deadline"] = parsed["deadline"]

        assignee_name = f"@{parsed['assignee_username']}" if parsed.get('assignee_username') else "ты"
        await update.message.reply_text(
            f"📌 *{parsed['task']}*\n"
            f"👤 {assignee_name}\n"
            f"📅 {format_date(parsed['deadline'])}\n\n"
            "🔄 Повторять?",
            parse_mode="Markdown",
            reply_markup=_build_recurrence_keyboard()
        )
        return States.TASK_RECURRENCE

    # Has assignee - ask for deadline
    if parsed.get("assignee_id"):
        context.user_data["task_assignee_id"] = parsed["assignee_id"]
        context.user_data["task_assignee_username"] = parsed["assignee_username"]

        assignee_name = f"@{parsed['assignee_username']}" if parsed.get('assignee_username') else "ты"
        await update.message.reply_text(
            f"📌 *{parsed['task']}*\n"
            f"👤 {assignee_name}\n\n"
            "📅 Когда? (завтра, каждый понедельник...)",
            parse_mode="Markdown"
        )
        return States.TASK_DEADLINE

    # Multiple candidates - show selection
    if parsed.get("multiple_candidates") and len(parsed["multiple_candidates"]) > 1:
        buttons = _build_user_selection_buttons(
            [type('User', (), c)() for c in parsed["multiple_candidates"]],
            "task_assignee"
        )
        buttons.append([InlineKeyboardButton("❌ Другой", callback_data="task_assignee:other")])

        await update.message.reply_text(
            "🤔 Нашёл несколько подходящих людей. Кого имел в виду?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return States.TASK_ASSIGNEE

    # Need assignee
    await update.message.reply_text(MSG_ASK_ASSIGNEE)
    return States.TASK_ASSIGNEE


async def receive_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task text from user."""
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Текст задачи не может быть пустым. Попробуй ещё раз:")
        return States.TASK_TEXT

    chat_id = context.user_data["task_chat_id"]
    author_id = context.user_data["task_author_id"]

    parsed = await _smart_parse_task(text, chat_id, author_id)
    context.user_data["task_text"] = parsed["task"][:settings.max_task_length]

    # Handle self-assignment
    if parsed.get("is_self") and not parsed.get("assignee_id"):
        async with get_session() as session:
            result = await session.execute(select(User).where(User.id == author_id))
            author = result.scalar_one_or_none()
            if author:
                parsed["assignee_id"] = author_id
                parsed["assignee_username"] = author.username

    return await _route_parsed_task(update, context, parsed)


async def receive_task_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task assignee from user."""
    text = update.message.text.strip()
    chat_id = context.user_data["task_chat_id"]
    user_id = update.effective_user.id

    # Check self-assignment
    if text.lower() in SELF_KEYWORDS:
        async with get_session() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()

            if user:
                context.user_data["task_assignee_id"] = user.id
                context.user_data["task_assignee_username"] = user.username

                await update.message.reply_text(MSG_ASK_DEADLINE)
                return States.TASK_DEADLINE

    # Check @username
    username_match = re.search(r"@(\w+)", text)

    async with get_session() as session:
        if username_match:
            username = username_match.group(1)
            user = await _find_user_by_username(session, username)

            if user:
                is_member = await is_user_in_chat(session, user.id, chat_id)
                if is_member:
                    context.user_data["task_assignee_id"] = user.id
                    context.user_data["task_assignee_username"] = username
                    await update.message.reply_text(MSG_ASK_DEADLINE)
                    return States.TASK_DEADLINE

        # Try fuzzy match by name
        members = await _get_chat_members(session, chat_id)
        matching = await _find_user_by_name_fuzzy(members, text)

        if len(matching) == 1:
            user = matching[0]
            context.user_data["task_assignee_id"] = user.id
            context.user_data["task_assignee_username"] = user.username

            await update.message.reply_text(
                f"👤 Исполнитель: @{user.username}\n\n{MSG_ASK_DEADLINE}"
            )
            return States.TASK_DEADLINE

        elif len(matching) > 1:
            buttons = _build_user_selection_buttons(matching, "task_assignee")
            buttons.append([InlineKeyboardButton("❌ Другой", callback_data="task_assignee:other")])

            await update.message.reply_text(
                "🤔 Нашёл несколько подходящих. Кого имел в виду?",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return States.TASK_ASSIGNEE

        # Try LLM for nicknames
        if settings.yandex_gpt_api_key or settings.openai_api_key:
            try:
                members_list = ", ".join([
                    f"{m.first_name or ''} {m.last_name or ''} (@{m.username})"
                    for m in members if m.username
                ])

                response = await _llm_match_name(text, members_list)
                found_match = re.search(r"@(\w+)", response)

                if found_match:
                    username = found_match.group(1)
                    for m in members:
                        if m.username and m.username.lower() == username.lower():
                            context.user_data["task_assignee_id"] = m.id
                            context.user_data["task_assignee_username"] = m.username

                            await update.message.reply_text(
                                f"👤 Исполнитель: @{m.username}\n\n{MSG_ASK_DEADLINE}"
                            )
                            return States.TASK_DEADLINE
            except Exception as e:
                logger.debug(f"LLM name matching failed: {e}")

    # Try Telegram API
    potential_username = text.strip().replace("@", "")
    if potential_username and potential_username.isalnum():
        try:
            chat_member = await context.bot.get_chat_member(chat_id, f"@{potential_username}")
            if chat_member and chat_member.user:
                user = chat_member.user
                async with get_session() as session:
                    db_user = await get_or_create_user(
                        session, user.id,
                        username=user.username,
                        first_name=user.first_name,
                        last_name=user.last_name
                    )
                    existing = await session.execute(
                        select(ChatMember).where(
                            ChatMember.chat_id == chat_id,
                            ChatMember.user_id == user.id
                        )
                    )
                    if not existing.scalar_one_or_none():
                        session.add(ChatMember(chat_id=chat_id, user_id=user.id))
                        await session.commit()

                context.user_data["task_assignee_id"] = user.id
                context.user_data["task_assignee_username"] = user.username or potential_username

                await update.message.reply_text(
                    f"👤 Исполнитель: @{user.username or potential_username}\n\n{MSG_ASK_DEADLINE}"
                )
                return States.TASK_DEADLINE
        except Exception as e:
            logger.debug(f"Telegram API user lookup failed: {e}")

    # Build helpful error message
    known_names = []
    async with get_session() as session:
        members = await _get_chat_members(session, chat_id)
        for m in members:
            name = m.first_name or ""
            if m.username:
                known_names.append(f"{name} (@{m.username})")

    hint = ""
    if known_names:
        hint = f"\n\nИзвестные мне участники:\n" + "\n".join(f"• {n}" for n in known_names[:5])

    await update.message.reply_text(
        f"🤷 Не нашёл «{text}» в чате.\n\n"
        f"Укажи точный @username (например: @Daviddobro88)"
        f"{hint}"
    )
    return States.TASK_ASSIGNEE


async def task_assignee_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle assignee selection from inline keyboard."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1] if len(data) > 1 else ""

    if action == "other":
        await query.edit_message_text(MSG_ASK_ASSIGNEE)
        return States.TASK_ASSIGNEE

    try:
        assignee_id = int(data[1])
        assignee_username = data[2] if len(data) > 2 else ""

        context.user_data["task_assignee_id"] = assignee_id
        context.user_data["task_assignee_username"] = assignee_username

        await query.edit_message_text(f"👤 Исполнитель: @{assignee_username}\n\n{MSG_ASK_DEADLINE}")
        return States.TASK_DEADLINE
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка. Попробуй снова: /task")
        return ConversationHandler.END


async def receive_task_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task deadline from user."""
    text = update.message.text.strip().lower()

    # Check for recurrence patterns first
    detected_recurrence = _detect_recurrence(text)

    if detected_recurrence:
        target_weekday = None
        for day_name, weekday in DAY_MAP.items():
            if day_name in text:
                target_weekday = weekday
                break

        default_hour = _detect_time_of_day(text)
        hour, minute = _parse_time_from_text(text)
        if hour is None:
            hour = default_hour
            minute = 0

        if target_weekday is not None:
            next_date = _calculate_next_weekday(target_weekday)
            deadline = datetime.combine(
                next_date, datetime.min.time().replace(hour=hour, minute=minute)
            )
        else:
            deadline = datetime.combine(
                date.today() + timedelta(days=1),
                datetime.min.time().replace(hour=hour, minute=minute)
            )

        context.user_data["task_deadline"] = deadline
        context.user_data["task_recurrence"] = detected_recurrence.value

        return await _create_task(update, context)

    # Regular deadline parsing
    try:
        deadline = parse_deadline(text)
        context.user_data["task_deadline"] = deadline

        await update.message.reply_text(
            "🔄 Повторять задачу?",
            reply_markup=_build_recurrence_keyboard()
        )
        return States.TASK_RECURRENCE

    except DateParseError as e:
        await update.message.reply_text(str(e))
        return States.TASK_DEADLINE


async def recurrence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle recurrence selection."""
    query = update.callback_query
    await query.answer()

    recurrence = query.data.split(":")[1]
    context.user_data["task_recurrence"] = recurrence

    await query.edit_message_text(f"🔄 Повтор: {_get_recurrence_label(recurrence)}")

    return await _create_task(update, context)


async def _create_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create the task after all data is collected."""
    chat_id = context.user_data["task_chat_id"]
    author_id = context.user_data["task_author_id"]
    text = context.user_data["task_text"]
    deadline = context.user_data["task_deadline"]
    assignee_username = context.user_data.get("task_assignee_username")
    assignee_id = context.user_data.get("task_assignee_id")
    command_message_id = context.user_data.get("task_command_message_id")
    recurrence_str = context.user_data.get("task_recurrence", "none")

    recurrence = _recurrence_str_to_enum(recurrence_str)

    async with get_session() as session:
        # Get assignee if we only have username
        if not assignee_id and assignee_username:
            assignee = await _find_user_by_username(session, assignee_username)
            if assignee:
                assignee_id = assignee.id

        if not assignee_id:
            if update.callback_query:
                await update.callback_query.edit_message_text(MSG_ASSIGNEE_NOT_FOUND)
            else:
                await update.message.reply_text(MSG_ASSIGNEE_NOT_FOUND)
            context.user_data.clear()
            return ConversationHandler.END

        task = Task(
            chat_id=chat_id,
            author_id=author_id,
            assignee_id=assignee_id,
            text=text,
            deadline=deadline,
            command_message_id=command_message_id,
            recurrence=recurrence,
        )
        session.add(task)
        await session.flush()

        result = await session.execute(select(User).where(User.id == assignee_id))
        assignee = result.scalar_one()

        deadline_str = format_date(deadline)
        recurrence_display = ""
        if recurrence != RecurrenceType.NONE:
            recurrence_display = f"\n🔄 Повтор: {_get_recurrence_label(recurrence.value)}"

        confirmation = (
            f'✅ Задача создана: "{text}"\n'
            f"Исполнитель: {assignee.display_name}\n"
            f"Дедлайн: {deadline_str}"
            f"{recurrence_display}"
        )

        if update.callback_query:
            await update.callback_query.edit_message_text(confirmation)
            reply = await context.bot.send_message(chat_id, "📌 Задача добавлена!")
        else:
            reply = await update.message.reply_text(confirmation)

        task.confirmation_message_id = reply.message_id

        # Notify assignee in DM
        if assignee_id != author_id:
            try:
                result = await session.execute(select(Chat).where(Chat.id == chat_id))
                chat = result.scalar_one()

                dm_text = (
                    f"📌 Новая задача!\n\n"
                    f'"{text}"\n'
                    f"Чат: {chat.title}\n"
                    f"Дедлайн: {deadline_str}"
                )

                keyboard = _build_task_action_keyboard(task.id, include_edit=False)

                await context.bot.send_message(
                    chat_id=assignee_id,
                    text=dm_text,
                    reply_markup=keyboard
                )
                task.is_delivered = True
            except Exception as e:
                logger.debug(f"Failed to send DM to assignee: {e}")
                await update.message.reply_text(
                    f"{assignee.display_name}, напиши мне в ЛС, "
                    "чтобы получать задачи и напоминания"
                )

    context.user_data.clear()
    return ConversationHandler.END


# --- Task List Handlers ---

async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tasks command - list active tasks in chat."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(MSG_GROUP_ONLY)
        return

    chat_id = update.effective_chat.id

    async with get_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.chat_id == chat_id,
                Task.status == TaskStatus.OPEN
            )
            .order_by(Task.deadline)
        )
        tasks = result.scalars().all()

        if not tasks:
            await update.message.reply_text(MSG_NO_ACTIVE_TASKS)
            return

        lines = ["📋 Активные задачи:\n"]

        for i, task in enumerate(tasks, 1):
            result = await session.execute(
                select(User).where(User.id == task.assignee_id)
            )
            task.assignee = result.scalar_one()
            lines.append(f"{i}. {format_task_short(task)}\n")

        await update.message.reply_text("\n".join(lines))


async def mytasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mytasks command - list user's tasks."""
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id if chat_type != "private" else None

    async with get_session() as session:
        if chat_id:
            # In group - show tasks in this chat only
            result = await session.execute(
                select(Task)
                .where(
                    Task.assignee_id == user_id,
                    Task.chat_id == chat_id,
                    Task.status == TaskStatus.OPEN
                )
                .order_by(Task.deadline)
            )
            tasks = result.scalars().all()

            if not tasks:
                await update.message.reply_text(MSG_NO_YOUR_TASKS_IN_CHAT)
                return

            lines = ["📋 Твои задачи в этом чате:\n"]

            for i, task in enumerate(tasks, 1):
                deadline_str = format_date(task.deadline)
                overdue = " ⚠️ просрочена" if task.is_overdue else ""
                lines.append(f"{i}. {task.text} | Дедлайн: {deadline_str}{overdue}")

            await update.message.reply_text("\n".join(lines))
        else:
            # In DM - show all tasks grouped by chat
            result = await session.execute(
                select(Task)
                .where(
                    Task.assignee_id == user_id,
                    Task.status == TaskStatus.OPEN
                )
                .order_by(Task.deadline)
            )
            tasks = result.scalars().all()

            if not tasks:
                await update.message.reply_text(MSG_NO_YOUR_TASKS)
                return

            # Group by chat
            by_chat = {}
            for task in tasks:
                if task.chat_id not in by_chat:
                    by_chat[task.chat_id] = []
                by_chat[task.chat_id].append(task)

            # Send each task as separate message with buttons
            for task_chat_id, chat_tasks in by_chat.items():
                result = await session.execute(
                    select(Chat).where(Chat.id == task_chat_id)
                )
                chat = result.scalar_one_or_none()
                chat_title = chat.title if chat else f"Чат {task_chat_id}"

                for task in chat_tasks:
                    result = await session.execute(
                        select(User).where(User.id == task.author_id)
                    )
                    author = result.scalar_one()

                    deadline_str = format_date(task.deadline)
                    overdue = "\n⚠️ Просрочена!" if task.is_overdue else ""

                    text = (
                        f"📌 {task.text}\n"
                        f"Чат: {chat_title}\n"
                        f"Автор: {author.display_name}\n"
                        f"Дедлайн: {deadline_str}{overdue}"
                    )

                    keyboard = _build_task_action_keyboard(task.id)
                    await update.message.reply_text(text, reply_markup=keyboard)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📋 Показать закрытые задачи",
                    callback_data="task:show_closed"
                )]
            ])
            await update.message.reply_text(
                "Это все твои активные задачи.",
                reply_markup=keyboard
            )


# --- Task Actions ---

async def done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /done command - close a task (reply to task message)."""
    if not update.message.reply_to_message:
        await update.message.reply_text(MSG_REPLY_TO_TASK)
        return

    reply_to = update.message.reply_to_message
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    async with get_session() as session:
        result = await session.execute(
            select(Task).where(
                and_(
                    Task.chat_id == chat_id,
                    (Task.command_message_id == reply_to.message_id) |
                    (Task.confirmation_message_id == reply_to.message_id)
                )
            )
        )
        task = result.scalar_one_or_none()

        if not task:
            await update.message.reply_text(MSG_NOT_A_TASK)
            return

        if task.status == TaskStatus.CLOSED:
            await update.message.reply_text(MSG_TASK_ALREADY_CLOSED)
            return

        if not await can_close_task(session, user_id, task):
            await update.message.reply_text(MSG_CANT_CLOSE)
            return

        task.status = TaskStatus.CLOSED
        task.closed_at = datetime.utcnow()
        task.closed_by = user_id

        next_task = await _create_next_recurring_task(session, task)

        result = await session.execute(select(User).where(User.id == user_id))
        closer = result.scalar_one()

        msg = f'✅ {closer.display_name} закрыл задачу "{task.text}"'
        if next_task:
            msg += f"\n🔄 Следующая: {format_date(next_task.deadline)}"

        await update.message.reply_text(msg)


async def _create_next_recurring_task(session, task: Task) -> Optional[Task]:
    """Create next instance of a recurring task."""
    if task.recurrence == RecurrenceType.NONE:
        return None

    current_deadline = task.deadline

    if task.recurrence == RecurrenceType.DAILY:
        next_deadline = current_deadline + timedelta(days=1)
    elif task.recurrence == RecurrenceType.WEEKDAYS:
        next_deadline = current_deadline + timedelta(days=1)
        while next_deadline.weekday() >= 5:  # Skip weekends
            next_deadline += timedelta(days=1)
    elif task.recurrence == RecurrenceType.WEEKLY:
        next_deadline = current_deadline + timedelta(weeks=1)
    elif task.recurrence == RecurrenceType.MONTHLY:
        next_deadline = current_deadline + relativedelta(months=1)
    else:
        return None

    new_task = Task(
        chat_id=task.chat_id,
        author_id=task.author_id,
        assignee_id=task.assignee_id,
        text=task.text,
        deadline=next_deadline,
        recurrence=task.recurrence,
        parent_task_id=task.parent_task_id or task.id,
    )
    session.add(new_task)
    await session.flush()

    return new_task


# --- Edit Handlers ---

async def edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /edit command - edit a task (reply to task message)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(MSG_DM_EDIT_HINT)
        return ConversationHandler.END

    if not update.message.reply_to_message:
        await update.message.reply_text(MSG_REPLY_TO_TASK)
        return ConversationHandler.END

    reply_to = update.message.reply_to_message
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args = " ".join(context.args) if context.args else ""

    async with get_session() as session:
        result = await session.execute(
            select(Task).where(
                and_(
                    Task.chat_id == chat_id,
                    (Task.command_message_id == reply_to.message_id) |
                    (Task.confirmation_message_id == reply_to.message_id)
                )
            )
        )
        task = result.scalar_one_or_none()

        if not task:
            await update.message.reply_text(MSG_NOT_A_TASK)
            return ConversationHandler.END

        if not await can_edit_task(session, user_id, task):
            await update.message.reply_text(MSG_CANT_EDIT)
            return ConversationHandler.END

        context.user_data["edit_task_id"] = task.id

        if args:
            return await _process_inline_edit(update, context, session, task, args)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Текст", callback_data=f"task:edit_field:text:{task.id}"),
                InlineKeyboardButton("Дедлайн", callback_data=f"task:edit_field:deadline:{task.id}"),
                InlineKeyboardButton("Исполнитель", callback_data=f"task:edit_field:assignee:{task.id}"),
            ]
        ])

        await update.message.reply_text("Что изменить?", reply_markup=keyboard)
        return ConversationHandler.END


async def _process_inline_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
    task: Task,
    args: str
) -> int:
    """Process inline edit command like '/edit дедлайн завтра'."""
    args_lower = args.lower()
    changes = []

    # Check for deadline
    if "дедлайн" in args_lower:
        deadline_text = args_lower.split("дедлайн", 1)[1].strip()
        for keyword in ["исполнитель", "текст"]:
            if keyword in deadline_text:
                deadline_text = deadline_text.split(keyword)[0].strip()

        try:
            new_deadline = parse_deadline(deadline_text)
            task.deadline = new_deadline
            changes.append(f"Новый дедлайн: {format_date(new_deadline)}")
        except DateParseError as e:
            await update.message.reply_text(f"Ошибка в дедлайне: {e}")
            return ConversationHandler.END

    # Check for assignee
    if "исполнитель" in args_lower:
        assignee_text = args_lower.split("исполнитель", 1)[1].strip()
        username_match = re.search(r"@?(\w+)", assignee_text)

        if username_match:
            username = username_match.group(1)
            new_assignee = await _find_user_by_username(session, username)

            if new_assignee:
                task.assignee_id = new_assignee.id
                changes.append(f"Новый исполнитель: {new_assignee.display_name}")
            else:
                await update.message.reply_text(MSG_USER_NOT_FOUND)
                return ConversationHandler.END

    # Check for text
    if "текст" in args_lower:
        text_idx = args.lower().find("текст")
        new_text = args[text_idx + 5:].strip()

        for keyword in ["дедлайн", "исполнитель"]:
            if keyword in new_text.lower():
                new_text = new_text[:new_text.lower().find(keyword)].strip()

        if new_text:
            task.text = new_text[:settings.max_task_length]
            changes.append(f'Новый текст: "{task.text}"')

    if not changes:
        await update.message.reply_text(
            "Неизвестное поле. Доступно: дедлайн, исполнитель, текст"
        )
        return ConversationHandler.END

    result = await session.execute(select(User).where(User.id == task.assignee_id))
    assignee = result.scalar_one()

    response = f'✏️ Задача изменена: "{task.text}"\n'
    response += "\n".join(changes)
    response += f"\n{assignee.display_name}, обрати внимание"

    await update.message.reply_text(response)

    context.user_data.clear()
    return ConversationHandler.END


async def receive_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive edited value from user."""
    task_id = context.user_data.get("edit_task_id")
    field = context.user_data.get("edit_field")
    value = update.message.text.strip()

    if not task_id or not field:
        context.user_data.clear()
        return ConversationHandler.END

    async with get_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            await update.message.reply_text("Задача не найдена")
            context.user_data.clear()
            return ConversationHandler.END

        if field == "text":
            task.text = value[:settings.max_task_length]
            await update.message.reply_text(f'✏️ Текст обновлён: "{task.text}"')

        elif field == "deadline":
            try:
                new_deadline = parse_deadline(value)
                task.deadline = new_deadline
                await update.message.reply_text(
                    f"✏️ Дедлайн обновлён: {format_date(new_deadline)}"
                )
            except DateParseError as e:
                await update.message.reply_text(str(e))
                return States.EDIT_VALUE

        elif field == "assignee":
            username_match = re.search(r"@?(\w+)", value)
            if username_match:
                username = username_match.group(1)
                new_assignee = await _find_user_by_username(session, username)

                if new_assignee:
                    task.assignee_id = new_assignee.id
                    await update.message.reply_text(
                        f"✏️ Исполнитель обновлён: {new_assignee.display_name}"
                    )
                else:
                    await update.message.reply_text(MSG_USER_NOT_FOUND)
                    return States.EDIT_VALUE

    context.user_data.clear()
    return ConversationHandler.END


# --- Callback Handlers ---

async def task_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle task-related callback queries."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "close":
        task_id = int(data[2])
        await _close_task_callback(update, context, task_id)

    elif action == "edit":
        task_id = int(data[2])
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Текст", callback_data=f"task:edit_field:text:{task_id}"),
                InlineKeyboardButton("Дедлайн", callback_data=f"task:edit_field:deadline:{task_id}"),
                InlineKeyboardButton("Исполнитель", callback_data=f"task:edit_field:assignee:{task_id}"),
            ],
            [InlineKeyboardButton("« Назад", callback_data=f"task:back:{task_id}")]
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)

    elif action == "edit_field":
        field = data[2]
        task_id = int(data[3])
        context.user_data["edit_task_id"] = task_id
        context.user_data["edit_field"] = field
        context.user_data["in_conversation"] = True

        prompts = {
            "text": "Введи новый текст задачи:",
            "deadline": "Введи новый дедлайн (например: завтра, в пятницу, 15.02):",
            "assignee": "Введи нового исполнителя (@username):",
        }

        await query.message.reply_text(prompts.get(field, "Введи новое значение:"))

    elif action == "show_closed":
        await _show_closed_tasks(update, context)

    elif action == "back":
        task_id = int(data[2])
        keyboard = _build_task_action_keyboard(task_id)
        await query.edit_message_reply_markup(reply_markup=keyboard)


async def _close_task_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    task_id: int
) -> None:
    """Close task from callback button."""
    query = update.callback_query
    user_id = update.effective_user.id

    async with get_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            await query.edit_message_text("Задача не найдена")
            return

        if task.status == TaskStatus.CLOSED:
            await query.edit_message_text(MSG_TASK_ALREADY_CLOSED)
            return

        if not await can_close_task(session, user_id, task):
            await query.answer(MSG_CANT_CLOSE, show_alert=True)
            return

        task.status = TaskStatus.CLOSED
        task.closed_at = datetime.utcnow()
        task.closed_by = user_id

        next_task = await _create_next_recurring_task(session, task)

        result = await session.execute(select(User).where(User.id == user_id))
        closer = result.scalar_one()

        msg = f'✅ Задача закрыта: "{task.text}"\nЗакрыл: {closer.display_name}'
        if next_task:
            msg += f"\n🔄 Следующая: {format_date(next_task.deadline)}"

        await query.edit_message_text(msg)

        # Notify in chat
        result = await session.execute(select(Chat).where(Chat.id == task.chat_id))
        chat = result.scalar_one()

        chat_msg = f'✅ {closer.display_name} закрыл задачу "{task.text}"'
        if next_task:
            chat_msg += f"\n🔄 Следующая: {format_date(next_task.deadline)}"

        try:
            await context.bot.send_message(chat_id=task.chat_id, text=chat_msg)
        except Exception as e:
            logger.debug(f"Failed to notify chat about closed task: {e}")


async def _show_closed_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's closed tasks."""
    query = update.callback_query
    user_id = update.effective_user.id

    cutoff = datetime.utcnow() - timedelta(days=settings.closed_tasks_retention_days)

    async with get_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.CLOSED,
                Task.closed_at >= cutoff
            )
            .order_by(Task.closed_at.desc())
            .limit(10)
        )
        tasks = result.scalars().all()

        if not tasks:
            await query.message.reply_text("Нет закрытых задач за последние 30 дней")
            return

        lines = ["📋 Закрытые задачи:\n"]

        for task in tasks:
            result = await session.execute(select(Chat).where(Chat.id == task.chat_id))
            chat = result.scalar_one_or_none()
            chat_title = chat.title if chat else "Неизвестный чат"

            closed_str = format_date(task.closed_at)
            lines.append(f"✓ {task.text}\n  Чат: {chat_title} | Закрыта: {closed_str}\n")

        await query.message.reply_text("\n".join(lines))


# --- Conversation Handlers ---

def get_task_conversation_handler() -> ConversationHandler:
    """Get conversation handler for task creation."""
    from handlers.start import cancel_handler

    return ConversationHandler(
        entry_points=[CommandHandler("task", task_handler)],
        states={
            States.TASK_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_text)
            ],
            States.TASK_ASSIGNEE: [
                CallbackQueryHandler(task_assignee_callback, pattern=r"^task_assignee:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_assignee)
            ],
            States.TASK_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_deadline)
            ],
            States.TASK_RECURRENCE: [
                CallbackQueryHandler(recurrence_callback, pattern=r"^recurrence:")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_chat=True,
        per_user=True,
    )


def get_edit_conversation_handler() -> ConversationHandler:
    """Get conversation handler for task editing."""
    from handlers.start import cancel_handler

    return ConversationHandler(
        entry_points=[CommandHandler("edit", edit_handler)],
        states={
            States.EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_chat=True,
        per_user=True,
    )
