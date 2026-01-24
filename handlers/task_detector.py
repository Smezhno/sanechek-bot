"""Task detection handler - analyzes messages for potential tasks."""
import logging
import re
from datetime import datetime, timedelta
from typing import TypedDict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Message, User, Task, ChatMember
from database.models import TaskStatus
from llm.client import ask_llm
from config import settings
from utils.date_parser import parse_deadline, DateParseError
from utils.formatters import format_date


logger = logging.getLogger(__name__)

# Constants
CHECK_INTERVAL_MESSAGES = 20
MIN_MESSAGES_FOR_ANALYSIS = 3
MIN_MESSAGE_LENGTH = 10
MAX_MESSAGES_TO_ANALYZE = 7
MAX_TASKS_TO_SHOW = 3
TASK_HASH_MODULO = 10000
MESSAGE_TRUNCATE_LENGTH = 150
BUTTON_TEXT_MAX_LENGTH = 25

# Messages
MSG_EXPIRED = "⏰ Предложение устарело"
MSG_DISMISSED = "👍 Окей, не буду"
MSG_NO_TASKS = "✅ Задач не обнаружено"
MSG_NO_API_KEY = "❌ API ключ не настроен"
MSG_TASK_CREATED = "✅ Задача создана!"


class SuggestedTaskData(TypedDict, total=False):
    """Structure for suggested task data stored in bot_data."""
    text: str
    assignee: str
    assignee_id: int
    assignee_name: str
    deadline: str
    chat_id: int


DETECTION_PROMPT = """Проанализируй сообщения и найди задачи.

Признаки задачи:
- "надо", "нужно", "необходимо" + действие
- просьба что-то сделать кому-то
- "доработать", "исправить", "добавить", "сделать"

НЕ задачи:
- Отчёты ("итог за сегодня", "что сделал")
- Статус-апдейты ("вчера сделал", "работаю над")

Сообщения для анализа:
{messages}

Инструкция:
1. Найди задачи в сообщениях выше
2. Перефразируй каждую коротко (2-5 слов)
3. Извлеки исполнителя если указан (@username или имя)

Формат ответа (СТРОГО без угловых скобок):
ЗАДАЧА: купить молоко | ИСПОЛНИТЕЛЬ: @ivan
ЗАДАЧА: починить кран | ИСПОЛНИТЕЛЬ: Вася
ЗАДАЧА: подготовить отчёт | ИСПОЛНИТЕЛЬ: не указан

Если задач нет, ответь одним словом: НЕТ"""

DETECTION_SYSTEM_PROMPT = "Ты анализатор задач. Находи все потенциальные задачи."


def _get_task_key(task_hash: str) -> str:
    """Get bot_data key for suggested task."""
    return f"suggested_task_{task_hash}"


def _get_task_data(context: ContextTypes.DEFAULT_TYPE, task_hash: str) -> SuggestedTaskData | None:
    """Get task data from bot_data by hash."""
    return context.bot_data.get(_get_task_key(task_hash))


def _store_task_data(context: ContextTypes.DEFAULT_TYPE, task_hash: str, data: SuggestedTaskData) -> None:
    """Store task data in bot_data."""
    context.bot_data[_get_task_key(task_hash)] = data


def _delete_task_data(context: ContextTypes.DEFAULT_TYPE, task_hash: str) -> None:
    """Delete task data from bot_data."""
    key = _get_task_key(task_hash)
    if key in context.bot_data:
        del context.bot_data[key]


def _truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate text to max length with suffix."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + suffix


def _compute_task_hash(text: str) -> str:
    """Compute hash for task text."""
    return str(abs(hash(text)) % TASK_HASH_MODULO)


def _is_template_response(text: str) -> bool:
    """Check if LLM returned a template instead of real task."""
    if "<" in text or ">" in text:
        return True
    lower = text.lower()
    return "формулировка" in lower or "пример" in lower


def _parse_llm_task_line(line: str) -> dict | None:
    """Parse single task line from LLM response.

    Expected format: ЗАДАЧА: текст | ИСПОЛНИТЕЛЬ: @username
    Returns dict with 'text' and 'assignee' or None if invalid.
    """
    if "ЗАДАЧА:" not in line.upper():
        return None

    parts = line.split("|")
    task_text = parts[0].split(":", 1)[1].strip() if ":" in parts[0] else ""

    if not task_text or len(task_text) <= 3:
        return None

    if _is_template_response(task_text):
        return None

    assignee = ""
    if len(parts) > 1 and "ИСПОЛНИТЕЛЬ:" in parts[1].upper():
        assignee = parts[1].split(":", 1)[1].strip() if ":" in parts[1] else ""
        if assignee.lower() in ["не указан", "не указано", ""]:
            assignee = ""

    return {"text": task_text, "assignee": assignee}


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse LLM response and extract tasks."""
    if "НЕТ" in response_text.upper() and "ЗАДАЧА" not in response_text.upper():
        return []

    tasks = []
    for line in response_text.split("\n"):
        task = _parse_llm_task_line(line)
        if task:
            tasks.append(task)

    return tasks[:MAX_TASKS_TO_SHOW]


async def _fetch_recent_messages(chat_id: int, hours: int = 1) -> tuple[list[Message], dict[int, User]]:
    """Fetch recent messages from chat with user data.

    Returns tuple of (messages, users_dict).
    """
    async with get_session() as session:
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        result = await session.execute(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.is_bot_command == False,
                Message.created_at >= cutoff
            )
            .order_by(Message.created_at.desc())
            .limit(15)
        )
        messages = list(reversed(result.scalars().all()))

        if not messages:
            return [], {}

        user_ids = list(set(m.user_id for m in messages))
        result = await session.execute(
            select(User).where(User.id.in_(user_ids))
        )
        users = {u.id: u for u in result.scalars().all()}

    return messages, users


def _format_messages_for_llm(messages: list[Message], users: dict[int, User]) -> str:
    """Format messages for LLM prompt."""
    formatted = []
    for msg in messages[-MAX_MESSAGES_TO_ANALYZE:]:
        user = users.get(msg.user_id)
        username = user.display_name if user else "?"
        text = _truncate_text(msg.text, MESSAGE_TRUNCATE_LENGTH)
        formatted.append(f"{username}: {text}")
    return "\n".join(formatted)


async def _call_llm_for_tasks(messages_text: str) -> list[dict]:
    """Call LLM to detect tasks in messages."""
    result_text = await ask_llm(
        question=DETECTION_PROMPT.format(messages=messages_text),
        system_prompt=DETECTION_SYSTEM_PROMPT,
        max_tokens=200,
        temperature=0.3
    )
    return _parse_llm_response(result_text)


def _build_task_buttons(
    tasks: list[dict],
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE
) -> tuple[str, InlineKeyboardMarkup]:
    """Build suggestion message and keyboard for detected tasks."""
    suggestion = "💡 Нашёл потенциальные задачи:\n\n"
    buttons = []

    for task in tasks:
        task_text = task["text"]
        assignee = task.get("assignee", "")

        assignee_part = f" 👤 {assignee}" if assignee else ""
        suggestion += f"📌 {task_text}{assignee_part}\n"

        task_hash = _compute_task_hash(task_text)

        _store_task_data(context, task_hash, {
            "text": task_text,
            "assignee": assignee,
            "deadline": "",
            "chat_id": chat_id,
        })

        button_text = _truncate_text(task_text, BUTTON_TEXT_MAX_LENGTH)
        buttons.append([
            InlineKeyboardButton(
                f"✅ Создать: {button_text}",
                callback_data=f"suggest_task:{task_hash}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("❌ Не надо", callback_data="suggest_task:dismiss")
    ])

    return suggestion, InlineKeyboardMarkup(buttons)


def _has_api_key() -> bool:
    """Check if any LLM API key is configured."""
    return bool(settings.openai_api_key or settings.yandex_gpt_api_key)


async def analyze_for_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze recent messages for potential tasks (background, every N messages)."""
    if not update.message or not update.message.text:
        return

    if len(update.message.text) < MIN_MESSAGE_LENGTH:
        return

    if update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id

    # Rate limiting: check every N messages
    counter_key = f"task_detector_{chat_id}"
    counter = context.bot_data.get(counter_key, 0) + 1
    context.bot_data[counter_key] = counter

    if counter < CHECK_INTERVAL_MESSAGES:
        return

    context.bot_data[counter_key] = 0

    if not _has_api_key():
        return

    messages, users = await _fetch_recent_messages(chat_id)

    if len(messages) < MIN_MESSAGES_FOR_ANALYSIS:
        return

    messages_text = _format_messages_for_llm(messages, users)

    try:
        tasks = await _call_llm_for_tasks(messages_text)

        if not tasks:
            return

        suggestion, keyboard = _build_task_buttons(tasks, chat_id, context)

        await update.message.reply_text(
            suggestion,
            reply_markup=keyboard
        )

    except Exception as e:
        # Silently fail - this is a background feature
        logger.debug(f"Task detection failed: {e}")


async def force_detect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force task detection (for testing via /detect command)."""
    chat_id = update.effective_chat.id

    await update.message.reply_text("🔍 Анализирую последние сообщения...")

    if not _has_api_key():
        await update.message.reply_text(MSG_NO_API_KEY)
        return

    messages, users = await _fetch_recent_messages(chat_id)

    if len(messages) < MIN_MESSAGES_FOR_ANALYSIS:
        await update.message.reply_text(
            f"📭 Недостаточно сообщений для анализа.\n"
            f"Найдено: {len(messages)}, нужно минимум: {MIN_MESSAGES_FOR_ANALYSIS}\n\n"
            "Напишите несколько сообщений в чат и попробуйте снова."
        )
        return

    messages_text = _format_messages_for_llm(messages, users)

    try:
        tasks = await _call_llm_for_tasks(messages_text)

        if not tasks:
            await update.message.reply_text(MSG_NO_TASKS)
            return

        suggestion, keyboard = _build_task_buttons(tasks, chat_id, context)

        await update.message.reply_text(
            suggestion,
            reply_markup=keyboard
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")


# --- Callback handlers ---

async def _handle_assignee_selection(
    query, context: ContextTypes.DEFAULT_TYPE, data: list[str]
) -> None:
    """Handle assignee selection from multiple matches."""
    assignee_id = int(data[2])
    task_hash = data[4]

    task_data = _get_task_data(context, task_hash)
    if not task_data:
        await query.edit_message_text(MSG_EXPIRED)
        return

    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.id == assignee_id)
        )
        assignee_user = result.scalar_one_or_none()

        if assignee_user:
            task_data["assignee_id"] = assignee_user.id
            task_data["assignee_name"] = assignee_user.display_name

            # Create task immediately without asking for deadline
            await _create_task_from_data(query, context, task_hash, task_data)


async def _handle_self_assign(
    query, context: ContextTypes.DEFAULT_TYPE, task_hash: str
) -> None:
    """Handle 'assign to self' action."""
    task_data = _get_task_data(context, task_hash)
    if not task_data:
        await query.edit_message_text(MSG_EXPIRED)
        return

    task_data["assignee_id"] = query.from_user.id
    task_data["assignee_name"] = query.from_user.first_name

    # Create task immediately without asking for deadline
    await _create_task_from_data(query, context, task_hash, task_data)


async def _handle_skip_assignee(
    query, context: ContextTypes.DEFAULT_TYPE, task_hash: str
) -> None:
    """Handle 'skip assignee' action."""
    task_data = _get_task_data(context, task_hash)
    if not task_data:
        await query.edit_message_text(MSG_EXPIRED)
        return

    # Create task immediately without asking for deadline
    await _create_task_from_data(query, context, task_hash, task_data)


async def _create_task_from_data(
    query, context: ContextTypes.DEFAULT_TYPE, task_hash: str, task_data: SuggestedTaskData = None
) -> None:
    """Create task from task_data without deadline."""
    if not task_data:
        task_data = _get_task_data(context, task_hash)
    
    if not task_data:
        await query.edit_message_text(MSG_EXPIRED)
        return

    async with get_session() as session:
        task = Task(
            chat_id=task_data["chat_id"],
            author_id=query.from_user.id,
            assignee_id=task_data.get("assignee_id"),
            text=task_data["text"],
            deadline=None,  # No deadline by default
            status=TaskStatus.OPEN
        )
        session.add(task)
        await session.commit()

        assignee_name = task_data.get("assignee_name", "Не назначен")
        await query.edit_message_text(
            f"{MSG_TASK_CREATED}\n\n"
            f"📌 {task_data['text']}\n"
            f"👤 {assignee_name}\n"
            f"📅 Срок не указан"
        )

    _delete_task_data(context, task_hash)


async def _handle_first_click(
    query, context: ContextTypes.DEFAULT_TYPE, task_hash: str
) -> None:
    """Handle first click on task suggestion - try to find assignee or ask."""
    task_data = _get_task_data(context, task_hash)
    if not task_data:
        await query.edit_message_text(MSG_EXPIRED)
        return

    # If assignee was extracted from context, try to find user
    if task_data.get("assignee"):
        assignee_text = task_data["assignee"]

        async with get_session() as session:
            assignee_result = await session.execute(
                select(User).where(
                    (User.username == assignee_text.replace('@', '')) |
                    (User.first_name.ilike(f"%{assignee_text}%")) |
                    (User.last_name.ilike(f"%{assignee_text}%"))
                )
            )
            assignee_user = assignee_result.scalar_one_or_none()

            if assignee_user:
                task_data["assignee_id"] = assignee_user.id
                task_data["assignee_name"] = assignee_user.display_name

                # Create task immediately without asking for deadline
                await _create_task_from_data(query, context, task_hash, task_data)
                return

    # No assignee found, ask for it
    await query.edit_message_text(
        f"📌 {task_data['text']}\n\n"
        f"👤 Кто исполнитель?\n"
        f"Ответь сообщением с @username или нажми кнопку:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Я сам", callback_data=f"suggest_task:self:{task_hash}")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data=f"suggest_task:skip_assignee:{task_hash}")]
        ])
    )
    context.user_data["waiting_assignee_for"] = task_hash


async def suggest_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle suggestion callback (button clicks)."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "assignee" and len(data) >= 5:
        await _handle_assignee_selection(query, context, data)
        return

    if action == "dismiss":
        await query.edit_message_text(MSG_DISMISSED)
        return

    task_hash = data[2] if len(data) > 2 else None

    if action == "self":
        await _handle_self_assign(query, context, task_hash)
        return

    if action == "skip_assignee":
        await _handle_skip_assignee(query, context, task_hash)
        return

    # First click - action is actually the task_hash
    await _handle_first_click(query, context, action)


# --- Message handlers for task details input ---

async def _find_user_by_username(session, username: str, chat_id: int) -> User | None:
    """Find user by @username and verify they're in the chat."""
    result = await session.execute(
        select(User).where(User.username == username)
    )
    user = result.scalar_one_or_none()

    if not user:
        return None

    # Verify user is in chat
    member_result = await session.execute(
        select(ChatMember).where(
            ChatMember.user_id == user.id,
            ChatMember.chat_id == chat_id,
            ChatMember.left_at.is_(None)
        )
    )
    if not member_result.scalar_one_or_none():
        return None

    return user


async def _find_users_by_name(session, name: str, chat_id: int) -> list[User]:
    """Find chat members matching name (fuzzy)."""
    members_result = await session.execute(
        select(User).join(ChatMember).where(
            ChatMember.chat_id == chat_id,
            ChatMember.left_at.is_(None)
        )
    )
    members = members_result.scalars().all()

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


async def _handle_assignee_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    assignee_hash: str
) -> None:
    """Handle text input for assignee selection."""
    task_data = _get_task_data(context, assignee_hash)
    if not task_data:
        return

    chat_id = task_data["chat_id"]

    async with get_session() as session:
        assignee_user = None

        # Try to find by @username
        if "@" in text:
            match = re.search(r"@(\w+)", text)
            if match:
                username = match.group(1)
                assignee_user = await _find_user_by_username(session, username, chat_id)

        # Try to find by name
        if not assignee_user:
            matching = await _find_users_by_name(session, text, chat_id)

            if len(matching) == 1:
                assignee_user = matching[0]
            elif len(matching) > 1:
                # Multiple matches - show buttons
                buttons = []
                for m in matching[:5]:
                    name = f"{m.first_name or ''} {m.last_name or ''}".strip()
                    buttons.append([
                        InlineKeyboardButton(
                            f"{name} (@{m.username})",
                            callback_data=f"suggest_task:assignee:{m.id}:{m.username}:{assignee_hash}"
                        )
                    ])

                await update.message.reply_text(
                    f"Найдено несколько совпадений для \"{text}\":\n"
                    "Выбери исполнителя:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

        if assignee_user:
            task_data["assignee_id"] = assignee_user.id
            task_data["assignee_name"] = assignee_user.display_name
        else:
            task_data["assignee_name"] = text

    del context.user_data["waiting_assignee_for"]

    # Create task immediately without asking for deadline
    async with get_session() as session:
        # Resolve assignee_id if we only have name
        assignee_id = task_data.get("assignee_id")
        if not assignee_id and task_data.get("assignee_name"):
            assignee_name = task_data["assignee_name"]
            if "@" in assignee_name:
                username = assignee_name.replace("@", "")
                result = await session.execute(
                    select(User).where(User.username == username)
                )
                assignee_user = result.scalar_one_or_none()
                if assignee_user:
                    assignee_id = assignee_user.id

        task = Task(
            chat_id=task_data["chat_id"],
            author_id=update.effective_user.id,
            assignee_id=assignee_id,
            text=task_data["text"],
            deadline=None,  # No deadline by default
            status=TaskStatus.OPEN
        )
        session.add(task)
        await session.commit()

        assignee_display = task_data.get("assignee_name", "Не назначен")
        await update.message.reply_text(
            f"{MSG_TASK_CREATED}\n\n"
            f"📌 {task_data['text']}\n"
            f"👤 {assignee_display}\n"
            f"📅 Срок не указан"
        )

    _delete_task_data(context, assignee_hash)


def _is_reply_to_bot_time_request(update: Update) -> bool:
    """Check if message is reply to bot asking for time (from other handlers)."""
    if not update.message.reply_to_message:
        return False

    reply_to = update.message.reply_to_message
    if not reply_to.from_user or not reply_to.from_user.is_bot:
        return False

    reply_text = reply_to.text or ""
    skip_phrases = ["когда напомнить", "укажи время", "дата уже прошла"]
    return any(phrase in reply_text.lower() for phrase in skip_phrases)


async def handle_task_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle assignee input for suggested tasks."""
    logger.info(
        f"handle_task_details invoked: "
        f"user_id={update.effective_user.id if update.effective_user else None}, "
        f"chat_id={update.effective_chat.id if update.effective_chat else None}"
    )

    if not update.message or not update.message.text:
        logger.debug("handle_task_details: no message or text")
        return

    text = update.message.text.strip()

    waiting_assignee = context.user_data.get("waiting_assignee_for")

    logger.info(
        f"handle_task_details: text='{text}', "
        f"waiting_assignee={waiting_assignee}, "
        f"user_data keys={list(context.user_data.keys())}"
    )

    if not waiting_assignee:
        logger.debug(f"handle_task_details: not waiting for assignee, text='{text}'")
        return

    if _is_reply_to_bot_time_request(update):
        logger.info("Skipping: reply to bot asking for time")
        return

    await _handle_assignee_input(update, context, text, waiting_assignee)
