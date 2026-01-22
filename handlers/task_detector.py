"""Task detection handler - analyzes messages for potential tasks."""
import random
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Message, Chat
from llm.client import ask_llm
from config import settings


# How often to check (not every message to save API calls)
CHECK_INTERVAL_MESSAGES = 20  # Check every N messages (increased to save tokens)
MIN_MESSAGES_FOR_ANALYSIS = 3  # Minimum messages to analyze
MIN_MESSAGE_LENGTH = 10  # Ignore very short messages
MAX_MESSAGES_TO_ANALYZE = 7  # Limit messages for analysis


def escape_markdown(text: str) -> str:
    """Escape special Markdown characters."""
    if not text:
        return ""
    # Escape special characters for MarkdownV2
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


DETECTION_PROMPT = """Проанализируй сообщения и найди потенциальные задачи.

Признаки задачи:
- "надо", "нужно", "необходимо" + действие
- просьба что-то сделать
- проблема, которую нужно решить
- "доработать", "исправить", "добавить", "сделать"
- "запланировать", "организовать", "подготовить"

НЕ считать задачами:
- Отчёты и резюме ("итог за сегодня", "результаты работы", "что было сделано")
- Статус-апдейты ("вчера сделал", "сегодня работаю над")
- Планы без конкретных действий ("в будущем", "когда-нибудь")
- Вопросы и обсуждения без конкретных задач

Сообщения:
{messages}

Для каждой найденной задачи:
1. Перефразируй её чётко и кратко (как task в Jira)
2. Если в сообщении указан исполнитель (@username или имя), извлеки его
3. Выведи в формате: ЗАДАЧА: <чёткая формулировка> | ИСПОЛНИТЕЛЬ: <@username или имя, или "не указан">

Если задач нет: НЕТ"""


async def analyze_for_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze recent messages for potential tasks."""
    # Skip if no text message (images, videos, stickers, etc.)
    if not update.message or not update.message.text:
        return
    
    # Skip very short messages
    if len(update.message.text) < MIN_MESSAGE_LENGTH:
        return
    
    # Only in groups
    if update.effective_chat.type == "private":
        return
    
    chat_id = update.effective_chat.id
    
    # Check if we should analyze (not every message)
    counter_key = f"task_detector_{chat_id}"
    counter = context.bot_data.get(counter_key, 0) + 1
    context.bot_data[counter_key] = counter
    
    if counter < CHECK_INTERVAL_MESSAGES:
        return
    
    # Reset counter
    context.bot_data[counter_key] = 0
    
    # Don't analyze if no API key
    if not settings.openai_api_key and not settings.yandex_gpt_api_key:
        return
    
    # Get recent messages
    async with get_session() as session:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        
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
        
        if len(messages) < MIN_MESSAGES_FOR_ANALYSIS:
            return
        
        # Get usernames
        from database import User
        user_ids = list(set(m.user_id for m in messages))
        result = await session.execute(
            select(User).where(User.id.in_(user_ids))
        )
        users = {u.id: u for u in result.scalars().all()}
    
    # Format messages (limit length to save tokens)
    formatted = []
    for msg in messages[-MAX_MESSAGES_TO_ANALYZE:]:
        user = users.get(msg.user_id)
        username = user.display_name if user else "?"
        # Truncate long messages
        text = msg.text[:150] + "..." if len(msg.text) > 150 else msg.text
        formatted.append(f"{username}: {text}")
    
    messages_text = "\n".join(formatted)
    
    # Call LLM
    try:
        result_text = await ask_llm(
            question=DETECTION_PROMPT.format(messages=messages_text),
            system_prompt="Ты анализатор задач. Находи все потенциальные задачи.",
            max_tokens=200,
            temperature=0.3
        )
        
        # Check if task was detected
        if "НЕТ" in result_text.upper() and "ЗАДАЧА" not in result_text.upper():
            return
        
        # Parse multiple tasks (each line with ЗАДАЧА:)
        tasks = []
        for line in result_text.split("\n"):
            if "ЗАДАЧА:" in line.upper():
                # Parse format: ЗАДАЧА: текст | ИСПОЛНИТЕЛЬ: @username
                parts = line.split("|")
                task_text = parts[0].split(":", 1)[1].strip() if ":" in parts[0] else ""
                assignee = ""
                if len(parts) > 1 and "ИСПОЛНИТЕЛЬ:" in parts[1].upper():
                    assignee = parts[1].split(":", 1)[1].strip() if ":" in parts[1] else ""
                    if assignee.lower() in ["не указан", "не указано", ""]:
                        assignee = ""
                
                if task_text and len(task_text) > 3:
                    tasks.append({
                        "text": task_text,
                        "assignee": assignee
                    })
        
        if not tasks:
            return
        
        # Build suggestion message for all tasks
        suggestion = f"💡 Нашёл потенциальные задачи:\n\n"
        
        buttons = []
        for i, task in enumerate(tasks[:3]):  # Max 3 tasks
            task_text = task["text"] if isinstance(task, dict) else task
            assignee = task.get("assignee", "") if isinstance(task, dict) else ""
            
            assignee_text = f" 👤 {assignee}" if assignee else ""
            suggestion += f"📌 *{escape_markdown(task_text)}*{escape_markdown(assignee_text)}\n"
            task_hash = abs(hash(task_text)) % 10000
            
            # Store task data for callback
            context.bot_data[f"suggested_task_{task_hash}"] = {
                "text": task_text,
                "assignee": assignee,
                "deadline": "",
                "chat_id": chat_id,
            }
            
            buttons.append([
                InlineKeyboardButton(
                    f"✅ Создать: {task_text[:25]}{'...' if len(task_text) > 25 else ''}", 
                    callback_data=f"suggest_task:{task_hash}"
                )
            ])
        
        buttons.append([
            InlineKeyboardButton("❌ Не надо", callback_data="suggest_task:dismiss")
        ])
        
        keyboard = InlineKeyboardMarkup(buttons)
        
        await update.message.reply_text(
            suggestion,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
    except Exception as e:
        # Silently fail - this is a background feature
        pass


async def force_detect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force task detection (for testing)."""
    chat_id = update.effective_chat.id
    
    await update.message.reply_text("🔍 Анализирую последние сообщения...")
    
    # Don't analyze if no API key
    if not settings.openai_api_key and not settings.yandex_gpt_api_key:
        await update.message.reply_text("❌ API ключ не настроен")
        return
    
    # Get recent messages
    async with get_session() as session:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        
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
        
        if len(messages) < MIN_MESSAGES_FOR_ANALYSIS:
            await update.message.reply_text(
                f"📭 Недостаточно сообщений для анализа.\n"
                f"Найдено: {len(messages)}, нужно минимум: {MIN_MESSAGES_FOR_ANALYSIS}\n\n"
                "Напишите несколько сообщений в чат и попробуйте снова."
            )
            return
        
        # Get usernames
        from database import User
        user_ids = list(set(m.user_id for m in messages))
        result = await session.execute(
            select(User).where(User.id.in_(user_ids))
        )
        users = {u.id: u for u in result.scalars().all()}
    
    # Format messages
    formatted = []
    for msg in messages[-MAX_MESSAGES_TO_ANALYZE:]:
        user = users.get(msg.user_id)
        username = user.display_name if user else "?"
        text = msg.text[:150] + "..." if len(msg.text) > 150 else msg.text
        formatted.append(f"{username}: {text}")
    
    messages_text = "\n".join(formatted)
    
    # Call LLM
    try:
        result_text = await ask_llm(
            question=DETECTION_PROMPT.format(messages=messages_text),
            system_prompt="Ты анализатор задач. Находи все потенциальные задачи.",
            max_tokens=200,
            temperature=0.3
        )
        
        # Check if task was detected
        if "НЕТ" in result_text.upper() and "ЗАДАЧА" not in result_text.upper():
            await update.message.reply_text("✅ Задач не обнаружено")
            return
        
        # Parse multiple tasks
        tasks = []
        for line in result_text.split("\n"):
            if "ЗАДАЧА:" in line.upper():
                # Parse format: ЗАДАЧА: текст | ИСПОЛНИТЕЛЬ: @username
                parts = line.split("|")
                task_text = parts[0].split(":", 1)[1].strip() if ":" in parts[0] else ""
                assignee = ""
                if len(parts) > 1 and "ИСПОЛНИТЕЛЬ:" in parts[1].upper():
                    assignee = parts[1].split(":", 1)[1].strip() if ":" in parts[1] else ""
                    if assignee.lower() in ["не указан", "не указано", ""]:
                        assignee = ""
                
                if task_text and len(task_text) > 3:
                    tasks.append({
                        "text": task_text,
                        "assignee": assignee
                    })
        
        if not tasks:
            await update.message.reply_text("✅ Задач не обнаружено")
            return
        
        # Build suggestion message
        suggestion = f"💡 Нашёл потенциальные задачи:\n\n"
        
        buttons = []
        for task in tasks[:3]:  # Max 3 tasks
            task_text = task["text"] if isinstance(task, dict) else task
            assignee = task.get("assignee", "") if isinstance(task, dict) else ""
            
            assignee_text = f" 👤 {assignee}" if assignee else ""
            suggestion += f"📌 *{escape_markdown(task_text)}*{escape_markdown(assignee_text)}\n"
            task_hash = abs(hash(task_text)) % 10000
            
            context.bot_data[f"suggested_task_{task_hash}"] = {
                "text": task_text,
                "assignee": assignee,
                "deadline": "",
                "chat_id": chat_id,
            }
            
            buttons.append([
                InlineKeyboardButton(
                    f"✅ Создать: {task_text[:25]}{'...' if len(task_text) > 25 else ''}", 
                    callback_data=f"suggest_task:{task_hash}"
                )
            ])
        
        buttons.append([
            InlineKeyboardButton("❌ Не надо", callback_data="suggest_task:dismiss")
        ])
        
        keyboard = InlineKeyboardMarkup(buttons)
        
        await update.message.reply_text(
            suggestion,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")


async def suggest_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle suggestion callback."""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split(":")
    action = data[1]
    
    # Handle assignee selection from multiple matches
    if action == "assignee" and len(data) >= 5:
        assignee_id = int(data[2])
        assignee_username = data[3]
        task_hash = data[4]
        
        task_data = context.bot_data.get(f"suggested_task_{task_hash}")
        if not task_data:
            await query.edit_message_text("⏰ Предложение устарело")
            return
        
        from database import get_session, User
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.id == assignee_id)
            )
            assignee_user = result.scalar_one_or_none()
            
            if assignee_user:
                task_data["assignee_id"] = assignee_user.id
                task_data["assignee_name"] = assignee_user.display_name
                
                await query.edit_message_text(
                    f"📌 *{escape_markdown(task_data['text'])}*\n"
                    f"👤 {escape_markdown(assignee_user.display_name)}\n\n"
                    f"⏰ Когда дедлайн?\n"
                    f"Ответь сообщением \\(например: завтра, через 3 дня\\)",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📅 Без дедлайна", callback_data=f"suggest_task:create_now:{task_hash}")]
                    ])
                )
                context.user_data["waiting_deadline_for"] = task_hash
        return
    
    if action == "dismiss":
        await query.edit_message_text("👍 Окей, не буду")
        return
    
    if action == "self":
        # Assign to self
        task_hash = data[2] if len(data) > 2 else None
        task_data = context.bot_data.get(f"suggested_task_{task_hash}")
        if not task_data:
            await query.edit_message_text("⏰ Предложение устарело")
            return
        
        task_data["assignee_id"] = query.from_user.id
        task_data["assignee_name"] = query.from_user.first_name
        
        await query.edit_message_text(
            f"📌 *{escape_markdown(task_data['text'])}*\n"
            f"👤 {escape_markdown(query.from_user.first_name)}\n\n"
            f"⏰ Когда дедлайн?\n"
            f"Ответь сообщением \\(например: завтра, через 3 дня\\)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Без дедлайна", callback_data=f"suggest_task:create_now:{task_hash}")]
            ])
        )
        context.user_data["waiting_deadline_for"] = task_hash
        return
    
    if action == "skip_assignee":
        # Skip assignee, ask for deadline
        task_hash = data[2] if len(data) > 2 else None
        task_data = context.bot_data.get(f"suggested_task_{task_hash}")
        if not task_data:
            await query.edit_message_text("⏰ Предложение устарело")
            return
        
        await query.edit_message_text(
            f"📌 *{escape_markdown(task_data['text'])}*\n\n"
            f"⏰ Когда дедлайн?\n"
            f"Ответь сообщением \\(например: завтра, через 3 дня, в пятницу\\)\n"
            f"Или нажми кнопку:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Без дедлайна", callback_data=f"suggest_task:create_now:{task_hash}")]
            ])
        )
        context.user_data["waiting_deadline_for"] = task_hash
        return
    
    if action == "create_now":
        # Create task without deadline
        task_hash = data[2] if len(data) > 2 else None
        task_data = context.bot_data.get(f"suggested_task_{task_hash}")
        if not task_data:
            await query.edit_message_text("⏰ Предложение устарело")
            return
        
        # Create the task
        from database import get_session, Task, User
        async with get_session() as session:
            task = Task(
                chat_id=task_data["chat_id"],
                creator_id=query.from_user.id,
                assignee_id=task_data.get("assignee_id"),
                text=task_data["text"],
                status="open"
            )
            session.add(task)
            await session.commit()
            
            await query.edit_message_text(
                f"✅ Задача создана!\n\n"
                f"📌 *{escape_markdown(task_data['text'])}*\n"
                f"👤 {escape_markdown(task_data.get('assignee_name', 'Не назначен'))}\n"
                f"📅 Без дедлайна",
                parse_mode="Markdown"
            )
        return
    
    # First click - check if assignee already extracted
    task_data = context.bot_data.get(f"suggested_task_{action}")
    
    if not task_data:
        await query.edit_message_text("⏰ Предложение устарело")
        return
    
    # If assignee was extracted from context, try to find user
    if task_data.get("assignee"):
        assignee_text = task_data["assignee"]
        from database import get_session, User
        from sqlalchemy import select
        
        async with get_session() as session:
            # Try to find by username or name
            assignee_result = await session.execute(
                select(User).where(
                    (User.username == assignee_text.replace('@', '')) |
                    (User.first_name.ilike(f"%{assignee_text}%")) |
                    (User.last_name.ilike(f"%{assignee_text}%"))
                )
            )
            assignee_user = assignee_result.scalar_one_or_none()
            
            if assignee_user:
                # Found user, skip to deadline
                task_data["assignee_id"] = assignee_user.id
                task_data["assignee_name"] = assignee_user.display_name
                
                await query.edit_message_text(
                    f"📌 *{escape_markdown(task_data['text'])}*\n"
                    f"👤 {escape_markdown(assignee_user.display_name)}\n\n"
                    f"⏰ Когда дедлайн?\n"
                    f"Ответь сообщением \\(например: завтра, через 3 дня\\)",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📅 Без дедлайна", callback_data=f"suggest_task:create_now:{action}")]
                    ])
                )
                context.user_data["waiting_deadline_for"] = action
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"Set waiting_deadline_for={action} for user {query.from_user.id}")
                return
    
    # No assignee found, ask for it
    await query.edit_message_text(
        f"📌 *{escape_markdown(task_data['text'])}*\n\n"
        f"👤 Кто исполнитель?\n"
        f"Ответь сообщением с @username или нажми кнопку:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Я сам", callback_data=f"suggest_task:self:{action}")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data=f"suggest_task:skip_assignee:{action}")]
        ])
    )
    context.user_data["waiting_assignee_for"] = action


async def handle_task_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle assignee/deadline input for suggested tasks."""
    import logging
    logger = logging.getLogger(__name__)
    
    # Log every call to see if handler is invoked
    logger.info(f"handle_task_details invoked: user_id={update.effective_user.id if update.effective_user else None}, chat_id={update.effective_chat.id if update.effective_chat else None}")
    
    if not update.message or not update.message.text:
        logger.debug("handle_task_details: no message or text")
        return
    
    text = update.message.text.strip()
    
    # Early return if not waiting for anything
    waiting_assignee = context.user_data.get("waiting_assignee_for")
    waiting_deadline = context.user_data.get("waiting_deadline_for")
    
    logger.info(f"handle_task_details: text='{text}', waiting_assignee={waiting_assignee}, waiting_deadline={waiting_deadline}, user_data keys={list(context.user_data.keys())}")
    
    if not waiting_assignee and not waiting_deadline:
        logger.debug(f"handle_task_details: not waiting for anything, text='{text}'")
        return
    
    # Skip if this is a reply to bot asking for time (from /ask handler)
    if update.message.reply_to_message:
        reply_to = update.message.reply_to_message
        if reply_to.from_user and reply_to.from_user.is_bot:
            reply_text = reply_to.text or ""
            if any(phrase in reply_text.lower() for phrase in [
                "когда напомнить", "укажи время", "дата уже прошла"
            ]):
                logger.info("Skipping: reply to bot asking for time")
                return
    
    # Check if waiting for assignee
    assignee_hash = context.user_data.get("waiting_assignee_for")
    if assignee_hash:
        task_data = context.bot_data.get(f"suggested_task_{assignee_hash}")
        if task_data:
            chat_id = task_data["chat_id"]
            from database import get_session, User, ChatMember
            from sqlalchemy import select
            import re
            
            async with get_session() as session:
                assignee_user = None
                
                # Try to find by @username
                if "@" in text:
                    match = re.search(r"@(\w+)", text)
                    if match:
                        username = match.group(1)
                        result = await session.execute(
                            select(User).where(User.username == username)
                        )
                        assignee_user = result.scalar_one_or_none()
                        
                        if assignee_user:
                            # Check if user is in chat
                            member_result = await session.execute(
                                select(ChatMember).where(
                                    ChatMember.user_id == assignee_user.id,
                                    ChatMember.chat_id == chat_id,
                                    ChatMember.left_at.is_(None)
                                )
                            )
                            if not member_result.scalar_one_or_none():
                                assignee_user = None
                
                # Try to find by name (fuzzy match)
                if not assignee_user:
                    # Get all chat members
                    members_result = await session.execute(
                        select(User).join(ChatMember).where(
                            ChatMember.chat_id == chat_id,
                            ChatMember.left_at.is_(None)
                        )
                    )
                    members = members_result.scalars().all()
                    
                    text_lower = text.lower().strip()
                    matching = []
                    
                    for m in members:
                        first = (m.first_name or "").lower()
                        last = (m.last_name or "").lower()
                        full = f"{first} {last}".strip()
                        
                        # Check various matches
                        if (text_lower == first or 
                            text_lower == last or 
                            text_lower == full or
                            text_lower in first or
                            first.startswith(text_lower)):
                            matching.append(m)
                    
                    if len(matching) == 1:
                        assignee_user = matching[0]
                    elif len(matching) > 1:
                        # Multiple matches - show buttons
                        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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
                    # Not found, save as text
                    task_data["assignee_name"] = text
            
            del context.user_data["waiting_assignee_for"]
            
            # Ask for deadline
            assignee_display = task_data.get("assignee_name", "Не указан")
            await update.message.reply_text(
                f"👍 Исполнитель: {assignee_display}\n\n"
                f"⏰ Когда дедлайн?\n"
                f"(например: завтра, через 3 дня, в пятницу)",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📅 Без дедлайна", callback_data=f"suggest_task:create_now:{assignee_hash}")]
                ])
            )
            context.user_data["waiting_deadline_for"] = assignee_hash
        return
    
    # Check if waiting for deadline
    deadline_hash = context.user_data.get("waiting_deadline_for")
    if deadline_hash:
        logger.info(f"Processing deadline input: text='{text}', hash={deadline_hash}")
        
        task_data = context.bot_data.get(f"suggested_task_{deadline_hash}")
        if not task_data:
            # Task data expired or not found
            logger.warning(f"Task data not found for hash {deadline_hash}")
            await update.message.reply_text("⏰ Предложение устарело. Создай задачу заново: /task")
            del context.user_data["waiting_deadline_for"]
            return
        
        # Task data found - parse deadline and create task
        from utils.date_parser import parse_deadline, DateParseError
        from database import get_session, Task, User
        from database.models import TaskStatus
        
        try:
            deadline = parse_deadline(text)
            logger.info(f"Parsed deadline: {deadline}")
        except DateParseError as e:
            logger.warning(f"Date parse error: {e}")
            await update.message.reply_text(
                f"❌ Не понял дату: {str(e)}\n\n"
                f"Попробуй ещё раз (например: завтра, через 3 дня, в пятницу в 16:00)"
            )
            return
        
        del context.user_data["waiting_deadline_for"]
        
        # Create the task
        async with get_session() as session:
            # Get assignee_id if we have assignee_name
            assignee_id = task_data.get("assignee_id")
            if not assignee_id and task_data.get("assignee_name"):
                # Try to find by name
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
                assignee_id=assignee_id or update.effective_user.id,
                text=task_data["text"],
                deadline=deadline,
                status=TaskStatus.OPEN
            )
            session.add(task)
            await session.commit()
            logger.info(f"Task created: id={task.id}, text='{task.text}', assignee_id={task.assignee_id}, deadline={task.deadline}")
            
            # Get assignee name for display
            if assignee_id:
                result = await session.execute(
                    select(User).where(User.id == assignee_id)
                )
                assignee_user = result.scalar_one_or_none()
                assignee_display = assignee_user.display_name if assignee_user else task_data.get("assignee_name", "Не назначен")
            else:
                assignee_display = task_data.get("assignee_name", "Не назначен")
            
            from utils.formatters import format_date
            deadline_str = format_date(deadline, include_time=True)
            
            await update.message.reply_text(
                f"✅ Задача создана!\n\n"
                f"📌 *{escape_markdown(task_data['text'])}*\n"
                f"👤 {escape_markdown(assignee_display)}\n"
                f"📅 {escape_markdown(deadline_str)}",
                parse_mode="Markdown"
            )
        
        # Clean up bot_data
        if f"suggested_task_{deadline_hash}" in context.bot_data:
            del context.bot_data[f"suggested_task_{deadline_hash}"]
        return

