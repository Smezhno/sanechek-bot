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


DETECTION_PROMPT = """Проанализируй сообщения и найди потенциальные задачи.

Признаки задачи:
- "надо", "нужно", "необходимо" + действие
- просьба что-то сделать
- проблема, которую нужно решить
- "доработать", "исправить", "добавить", "сделать"

Сообщения:
{messages}

Для каждой найденной задачи:
1. Перефразируй её чётко и кратко (как task в Jira)
2. Выведи в формате: ЗАДАЧА: <чёткая формулировка>

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
                task_text = line.split(":", 1)[1].strip() if ":" in line else ""
                if task_text and len(task_text) > 3:
                    tasks.append(task_text)
        
        if not tasks:
            return
        
        # Build suggestion message for all tasks
        suggestion = f"💡 Нашёл потенциальные задачи:\n\n"
        
        buttons = []
        for i, task_text in enumerate(tasks[:3]):  # Max 3 tasks
            suggestion += f"📌 *{task_text}*\n"
            task_hash = abs(hash(task_text)) % 10000
            
            # Store task data for callback
            context.bot_data[f"suggested_task_{task_hash}"] = {
                "text": task_text,
                "assignee": "",
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
                task_text = line.split(":", 1)[1].strip() if ":" in line else ""
                if task_text and len(task_text) > 3:
                    tasks.append(task_text)
        
        if not tasks:
            await update.message.reply_text("✅ Задач не обнаружено")
            return
        
        # Build suggestion message
        suggestion = f"💡 Нашёл потенциальные задачи:\n\n"
        
        buttons = []
        for task_text in tasks[:3]:  # Max 3 tasks
            suggestion += f"📌 *{task_text}*\n"
            task_hash = abs(hash(task_text)) % 10000
            
            context.bot_data[f"suggested_task_{task_hash}"] = {
                "text": task_text,
                "assignee": "",
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
            f"📌 *{task_data['text']}*\n"
            f"👤 {query.from_user.first_name}\n\n"
            f"⏰ Когда дедлайн?\n"
            f"Ответь сообщением (например: завтра, через 3 дня)",
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
            f"📌 *{task_data['text']}*\n\n"
            f"⏰ Когда дедлайн?\n"
            f"Ответь сообщением (например: завтра, через 3 дня, в пятницу)\n"
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
                f"📌 *{task_data['text']}*\n"
                f"👤 {task_data.get('assignee_name', 'Не назначен')}\n"
                f"📅 Без дедлайна",
                parse_mode="Markdown"
            )
        return
    
    # First click - ask for assignee
    task_data = context.bot_data.get(f"suggested_task_{action}")
    
    if not task_data:
        await query.edit_message_text("⏰ Предложение устарело")
        return
    
    await query.edit_message_text(
        f"📌 *{task_data['text']}*\n\n"
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
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    
    # Check if waiting for assignee
    assignee_hash = context.user_data.get("waiting_assignee_for")
    if assignee_hash:
        task_data = context.bot_data.get(f"suggested_task_{assignee_hash}")
        if task_data:
            # Extract @username
            if "@" in text:
                import re
                match = re.search(r"@(\w+)", text)
                if match:
                    task_data["assignee_name"] = f"@{match.group(1)}"
            else:
                task_data["assignee_name"] = text
            
            del context.user_data["waiting_assignee_for"]
            
            # Ask for deadline
            await update.message.reply_text(
                f"👍 Исполнитель: {task_data['assignee_name']}\n\n"
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
        task_data = context.bot_data.get(f"suggested_task_{deadline_hash}")
        if task_data:
            from utils.date_parser import parse_datetime
            deadline = parse_datetime(text)
            
            del context.user_data["waiting_deadline_for"]
            
            # Create the task
            from database import get_session, Task
            async with get_session() as session:
                task = Task(
                    chat_id=task_data["chat_id"],
                    creator_id=update.effective_user.id,
                    text=task_data["text"],
                    status="open",
                    deadline=deadline
                )
                session.add(task)
                await session.commit()
                
                deadline_str = deadline.strftime("%d.%m.%Y %H:%M") if deadline else "Без дедлайна"
                
                await update.message.reply_text(
                    f"✅ Задача создана!\n\n"
                    f"📌 *{task_data['text']}*\n"
                    f"👤 {task_data.get('assignee_name', 'Не назначен')}\n"
                    f"📅 {deadline_str}",
                    parse_mode="Markdown"
                )
        return

