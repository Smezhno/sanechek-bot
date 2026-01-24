"""Reply intent analysis - determines what user wants to do with a reply to bot message."""
import json
import logging
from typing import Optional, Dict, Any, Tuple

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Task, Reminder
from llm.client import ask_llm
from llm.intent_prompts import REPLY_INTENT_PROMPT, REPLY_INTENT_SYSTEM
from utils.intent_helpers import IntentType


logger = logging.getLogger(__name__)


class ReplyIntentResult:
    """Result of reply intent analysis."""
    
    def __init__(
        self,
        intent_type: IntentType,
        confidence: float,
        edit_field: Optional[str] = None,
        new_value: Optional[str] = None,
        question: Optional[str] = None
    ):
        self.intent_type = intent_type
        self.confidence = confidence
        self.edit_field = edit_field
        self.new_value = new_value
        self.question = question
    
    def __repr__(self):
        return (
            f"ReplyIntentResult(type={self.intent_type.value}, "
            f"confidence={self.confidence:.2f}, "
            f"field={self.edit_field})"
        )


async def _identify_message_type(
    message_id: int,
    chat_id: int,
    session
) -> Tuple[Optional[str], Optional[Any]]:
    """
    Identify the type of bot message (task/reminder/answer) by looking in DB.
    
    Returns:
        Tuple of (message_type, related_object) where:
        - message_type: "task", "reminder", or "answer"
        - related_object: Task, Reminder, or None
    """
    # Check if it's a task message
    result = await session.execute(
        select(Task).where(
            (Task.command_message_id == message_id) |
            (Task.confirmation_message_id == message_id)
        )
    )
    task = result.scalar_one_or_none()
    if task:
        return "task", task
    
    # Check if it's a reminder message
    result = await session.execute(
        select(Reminder).where(
            (Reminder.command_message_id == message_id) |
            (Reminder.confirmation_message_id == message_id)
        )
    )
    reminder = result.scalar_one_or_none()
    if reminder:
        return "reminder", reminder
    
    # Otherwise it's probably an answer/question
    return "answer", None


async def _classify_reply_intent_with_llm(
    bot_message: str,
    user_reply: str,
    message_type: str
) -> Optional[ReplyIntentResult]:
    """Use LLM to classify reply intent."""
    try:
        prompt = REPLY_INTENT_PROMPT.format(
            bot_message=bot_message[:200],  # Truncate to save tokens
            user_reply=user_reply,
            message_type=message_type
        )
        
        response = await ask_llm(
            question=prompt,
            system_prompt=REPLY_INTENT_SYSTEM,
            max_tokens=150,
            temperature=0.3
        )
        
        # Parse JSON response
        response_clean = response.strip()
        if response_clean.startswith("```json"):
            response_clean = response_clean[7:]
        if response_clean.endswith("```"):
            response_clean = response_clean[:-3]
        
        data = json.loads(response_clean.strip())
        
        # Map intent string to enum
        intent_str = data.get("intent", "NONE").upper()
        intent_map = {
            "EDIT_TASK": IntentType.EDIT_TASK,
            "EDIT_REMINDER": IntentType.EDIT_REMINDER,
            "CLOSE_TASK": IntentType.CLOSE_TASK,
            "CONTINUE_DIALOG": IntentType.QUESTION,
            "NONE": IntentType.NONE
        }
        
        intent_type = intent_map.get(intent_str, IntentType.NONE)
        confidence = float(data.get("confidence", 0.0))
        
        return ReplyIntentResult(
            intent_type=intent_type,
            confidence=confidence,
            edit_field=data.get("edit_field"),
            new_value=data.get("new_value"),
            question=data.get("question")
        )
    
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse reply intent JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"Reply intent classification error: {e}")
        return None


async def analyze_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> Optional[ReplyIntentResult]:
    """
    Analyze user's reply to bot message to determine intent.
    
    Returns:
        ReplyIntentResult with detected intent, or None if cannot analyze
    """
    if not update.message or not update.message.reply_to_message:
        return None
    
    message = update.message
    reply_to = message.reply_to_message
    
    # Check if replying to bot
    if not reply_to.from_user or not reply_to.from_user.is_bot:
        return None
    
    if reply_to.from_user.id != context.bot.id:
        return None
    
    user_reply = message.text.strip()
    bot_message = reply_to.text if reply_to.text else ""
    
    # Identify message type from database
    async with get_session() as session:
        message_type, related_object = await _identify_message_type(
            reply_to.message_id,
            message.chat_id,
            session
        )
    
    # Use LLM to classify intent
    result = await _classify_reply_intent_with_llm(
        bot_message,
        user_reply,
        message_type
    )
    
    if result:
        logger.info(
            f"Reply intent detected: {result.intent_type.value} "
            f"(confidence: {result.confidence:.2f}) for {message_type}"
        )
    
    return result


async def get_reply_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> Optional[Dict[str, Any]]:
    """
    Get full context for a reply (message type and related object).
    
    Returns:
        Dict with 'message_type', 'task', 'reminder', etc.
    """
    if not update.message or not update.message.reply_to_message:
        return None
    
    reply_to = update.message.reply_to_message
    
    # Check if replying to bot
    if not reply_to.from_user or not reply_to.from_user.is_bot:
        return None
    
    if reply_to.from_user.id != context.bot.id:
        return None
    
    # Get message type and related object
    async with get_session() as session:
        message_type, related_object = await _identify_message_type(
            reply_to.message_id,
            update.message.chat_id,
            session
        )
    
    return {
        "message_type": message_type,
        "task": related_object if message_type == "task" else None,
        "reminder": related_object if message_type == "reminder" else None,
        "bot_message": reply_to.text if reply_to.text else "",
        "user_reply": update.message.text.strip()
    }

