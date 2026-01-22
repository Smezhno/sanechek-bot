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
CHECK_INTERVAL_MESSAGES = 10  # Check every N messages
MIN_MESSAGES_FOR_ANALYSIS = 5  # Minimum messages to analyze


DETECTION_PROMPT = """Проанализируй последние сообщения из рабочего чата и определи, есть ли там задача, которую стоит зафиксировать.

Признаки задачи:
- Кто-то просит что-то сделать
- Есть договорённость о действии
- Упоминается дедлайн или срок
- Кто-то берёт на себя обязательство

Сообщения:
{messages}

Если есть потенциальная задача, ответь в формате:
ЗАДАЧА: <краткое описание задачи>
ИСПОЛНИТЕЛЬ: <@username или "не указан">
СРОК: <срок или "не указан">

Если задачи нет, ответь только: НЕТ

Отвечай кратко, без лишних объяснений."""


async def analyze_for_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze recent messages for potential tasks."""
    if not update.message or not update.message.text:
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
    
    # Format messages
    formatted = []
    for msg in messages[-10:]:  # Last 10 messages
        user = users.get(msg.user_id)
        username = user.display_name if user else "Unknown"
        formatted.append(f"{username}: {msg.text}")
    
    messages_text = "\n".join(formatted)
    
    # Call LLM
    try:
        client = get_client()
        
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "user", "content": DETECTION_PROMPT.format(messages=messages_text)}
            ],
            max_tokens=200,
            temperature=0.3,
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Check if task was detected
        if result_text.upper().startswith("НЕТ"):
            return
        
        if "ЗАДАЧА:" not in result_text.upper():
            return
        
        # Parse result
        lines = result_text.split("\n")
        task_text = ""
        assignee = ""
        deadline = ""
        
        for line in lines:
            line_upper = line.upper()
            if line_upper.startswith("ЗАДАЧА:"):
                task_text = line.split(":", 1)[1].strip()
            elif line_upper.startswith("ИСПОЛНИТЕЛЬ:"):
                assignee = line.split(":", 1)[1].strip()
            elif line_upper.startswith("СРОК:"):
                deadline = line.split(":", 1)[1].strip()
        
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

