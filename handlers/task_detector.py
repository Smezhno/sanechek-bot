"""Task detection handler - analyzes messages for potential tasks."""
import random
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Message, Chat
from llm.client import get_client
from config import settings


# How often to check (not every message to save API calls)
CHECK_INTERVAL_MESSAGES = 20  # Check every N messages (increased to save tokens)
MIN_MESSAGES_FOR_ANALYSIS = 3  # Minimum messages to analyze
MIN_MESSAGE_LENGTH = 10  # Ignore very short messages
MAX_MESSAGES_TO_ANALYZE = 7  # Limit messages for analysis


DETECTION_PROMPT = """Есть ли задача в этих сообщениях?

{messages}

Если да: ЗАДАЧА: <что> | @кто | срок
Если нет: НЕТ"""


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
    if not settings.openai_api_key:
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
    
    # Call LLM with minimal tokens
    try:
        client = get_client()
        
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "user", "content": DETECTION_PROMPT.format(messages=messages_text)}
            ],
            max_tokens=100,  # Reduced from 200
            temperature=0.2,
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Check if task was detected
        if "НЕТ" in result_text.upper() or "ЗАДАЧА" not in result_text.upper():
            return
        
        # Parse compact format: ЗАДАЧА: <что> | @кто | срок
        task_text = ""
        assignee = ""
        deadline = ""
        
        if "ЗАДАЧА:" in result_text.upper():
            content = result_text.split(":", 1)[1].strip()
            parts = [p.strip() for p in content.split("|")]
            
            if len(parts) >= 1:
                task_text = parts[0]
            if len(parts) >= 2:
                assignee = parts[1]
            if len(parts) >= 3:
                deadline = parts[2]
        
        if not task_text:
            return
        
        # Build suggestion message
        suggestion = f"💡 Кажется, тут есть задача:\n\n"
        suggestion += f"📌 *{task_text}*\n"
        
        if assignee and assignee.lower() != "не указан":
            suggestion += f"👤 {assignee}\n"
        if deadline and deadline.lower() != "не указан":
            suggestion += f"📅 {deadline}\n"
        
        # Build command for quick task creation
        task_cmd = f"/task {task_text}"
        if assignee and "@" in assignee:
            task_cmd += f" {assignee}"
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Создать задачу", 
                    callback_data=f"suggest_task:{hash(task_text) % 10000}"
                ),
                InlineKeyboardButton(
                    "❌ Не надо",
                    callback_data="suggest_task:dismiss"
                )
            ]
        ])
        
        # Store task data for callback
        context.bot_data[f"suggested_task_{hash(task_text) % 10000}"] = {
            "text": task_text,
            "assignee": assignee if "@" in assignee else "",
            "deadline": deadline if deadline.lower() != "не указан" else "",
            "chat_id": chat_id,
        }
        
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
    if not settings.openai_api_key:
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
        client = get_client()
        
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "user", "content": DETECTION_PROMPT.format(messages=messages_text)}
            ],
            max_tokens=100,
            temperature=0.2,
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Check if task was detected
        if "НЕТ" in result_text.upper() or "ЗАДАЧА" not in result_text.upper():
            await update.message.reply_text("✅ Задач не обнаружено")
            return
        
        # Parse compact format
        task_text = ""
        assignee = ""
        deadline = ""
        
        if "ЗАДАЧА:" in result_text.upper():
            content = result_text.split(":", 1)[1].strip()
            parts = [p.strip() for p in content.split("|")]
            
            if len(parts) >= 1:
                task_text = parts[0]
            if len(parts) >= 2:
                assignee = parts[1]
            if len(parts) >= 3:
                deadline = parts[2]
        
        if not task_text:
            await update.message.reply_text("✅ Задач не обнаружено")
            return
        
        # Build suggestion
        suggestion = f"💡 Кажется, тут есть задача:\n\n"
        suggestion += f"📌 *{task_text}*\n"
        
        if assignee and assignee.lower() != "не указан":
            suggestion += f"👤 {assignee}\n"
        if deadline and deadline.lower() != "не указан":
            suggestion += f"📅 {deadline}\n"
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Создать задачу", 
                    callback_data=f"suggest_task:{hash(task_text) % 10000}"
                ),
                InlineKeyboardButton(
                    "❌ Не надо",
                    callback_data="suggest_task:dismiss"
                )
            ]
        ])
        
        context.bot_data[f"suggested_task_{hash(task_text) % 10000}"] = {
            "text": task_text,
            "assignee": assignee if "@" in assignee else "",
            "deadline": deadline if deadline.lower() != "не указан" else "",
            "chat_id": chat_id,
        }
        
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
    
    # Get stored task data
    task_data = context.bot_data.get(f"suggested_task_{action}")
    
    if not task_data:
        await query.edit_message_text("⏰ Предложение устарело")
        return
    
    # Build instruction
    cmd = f"/task {task_data['text']}"
    if task_data['assignee']:
        cmd += f" {task_data['assignee']}"
    if task_data['deadline']:
        cmd += f" {task_data['deadline']}"
    
    await query.edit_message_text(
        f"👍 Отправь команду:\n\n`{cmd}`",
        parse_mode="Markdown"
    )

