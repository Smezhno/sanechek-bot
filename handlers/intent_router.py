"""Intent classification and routing for natural language commands."""
import json
import logging
import re
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from llm.client import ask_llm
from llm.intent_prompts import (
    INTENT_CLASSIFICATION_PROMPT,
    INTENT_CLASSIFICATION_SYSTEM
)
from utils.intent_helpers import (
    IntentType,
    IntentResult,
    is_simple_action,
    needs_confirmation,
    should_ignore,
    format_confirmation_message
)
from config import settings


logger = logging.getLogger(__name__)


# Minimum message length to analyze
MIN_MESSAGE_LENGTH = 10

# Intent hash for storing pending confirmations
INTENT_HASH_MODULO = 10000


def _compute_intent_hash(text: str, chat_id: int) -> str:
    """Compute hash for pending intent confirmation."""
    return str(abs(hash(f"{text}:{chat_id}")) % INTENT_HASH_MODULO)


def _store_pending_intent(
    context: ContextTypes.DEFAULT_TYPE,
    intent_hash: str,
    intent_result: IntentResult,
    chat_id: int,
    user_id: int
) -> None:
    """Store pending intent for confirmation."""
    key = f"pending_intent_{intent_hash}"
    context.bot_data[key] = {
        "intent_type": intent_result.intent_type.value,
        "extracted_data": intent_result.extracted_data,
        "chat_id": chat_id,
        "user_id": user_id
    }


def _get_pending_intent(
    context: ContextTypes.DEFAULT_TYPE,
    intent_hash: str
) -> Optional[Dict[str, Any]]:
    """Get pending intent data."""
    key = f"pending_intent_{intent_hash}"
    return context.bot_data.get(key)


def _delete_pending_intent(context: ContextTypes.DEFAULT_TYPE, intent_hash: str) -> None:
    """Delete pending intent data."""
    key = f"pending_intent_{intent_hash}"
    context.bot_data.pop(key, None)


class RulesEngine:
    """Fast pattern-based intent classification."""
    
    # Task patterns
    TASK_PATTERNS = [
        r'\b(надо|нужно|необходимо)\b',
        r'\b(сделать|доработать|исправить|добавить|починить)\b',
        r'\b(можешь|можете)\s+\w+\s+(сделать|добавить|исправить)',
        r'\b(давай|давайте)\s+\w+\s+(сделаем|добавим|исправим)',
    ]
    
    # Reminder patterns
    REMINDER_PATTERNS = [
        r'\b(напомни|напомнить|напоминание)\b',
        r'\bчерез\s+\d+\s+(минут|час|день|дн)',
        r'\bчерез\s+(полчаса|час|день)',
        r'\b(завтра|послезавтра)\b.*\bнапомни',
    ]
    
    # Question patterns
    QUESTION_PATTERNS = [
        r'^\s*(как|что|почему|зачем|где|когда|кто|какой|какая|какие)\b',
        r'\b(можешь|можете)\s+(помочь|объяснить|рассказать|подсказать)',
        r'\b(помоги|помогите|подскажи|подскажите|объясни)\b',
        r'\?$',  # Ends with question mark
    ]
    
    @classmethod
    def classify(cls, text: str) -> Optional[IntentResult]:
        """
        Fast pattern-based classification.
        Returns IntentResult if confident, None otherwise.
        """
        text_lower = text.lower().strip()
        
        # Check for tasks
        for pattern in cls.TASK_PATTERNS:
            if re.search(pattern, text_lower):
                return IntentResult(
                    intent_type=IntentType.TASK,
                    confidence=0.75,
                    extracted_data={"task_text": text.strip()},
                    needs_confirmation=False
                )
        
        # Check for reminders
        for pattern in cls.REMINDER_PATTERNS:
            if re.search(pattern, text_lower):
                return IntentResult(
                    intent_type=IntentType.REMINDER,
                    confidence=0.75,
                    extracted_data={"reminder_text": text.strip()},
                    needs_confirmation=False
                )
        
        # Check for questions
        for pattern in cls.QUESTION_PATTERNS:
            if re.search(pattern, text_lower):
                return IntentResult(
                    intent_type=IntentType.QUESTION,
                    confidence=0.80,
                    extracted_data={"question": text.strip()},
                    needs_confirmation=False
                )
        
        return None


async def _classify_with_llm(text: str, context: str = "группа") -> Optional[IntentResult]:
    """
    Use LLM to classify intent for ambiguous cases.
    Returns IntentResult or None if classification fails.
    """
    try:
        prompt = INTENT_CLASSIFICATION_PROMPT.format(
            text=text,
            context=context
        )
        
        response = await ask_llm(
            question=prompt,
            system_prompt=INTENT_CLASSIFICATION_SYSTEM,
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
            "TASK": IntentType.TASK,
            "REMINDER": IntentType.REMINDER,
            "QUESTION": IntentType.QUESTION,
            "NONE": IntentType.NONE
        }
        
        intent_type = intent_map.get(intent_str, IntentType.NONE)
        confidence = float(data.get("confidence", 0.0))
        
        # Extract relevant data based on intent type
        extracted_data = {}
        if intent_type == IntentType.TASK:
            extracted_data = {
                "task_text": data.get("task_text", text),
                "assignee": data.get("assignee", ""),
                "deadline": data.get("deadline", "")
            }
        elif intent_type == IntentType.REMINDER:
            extracted_data = {
                "reminder_text": data.get("reminder_text", text),
                "reminder_time": data.get("reminder_time", "")
            }
        elif intent_type == IntentType.QUESTION:
            extracted_data = {
                "question": data.get("question", text)
            }
        
        return IntentResult(
            intent_type=intent_type,
            confidence=confidence,
            extracted_data=extracted_data
        )
    
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM JSON response: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM classification error: {e}")
        return None


async def classify_intent(text: str, context: str = "группа") -> Optional[IntentResult]:
    """
    Main intent classification function using hybrid approach.
    
    Args:
        text: User message text
        context: Context type ("группа" or "личное")
    
    Returns:
        IntentResult or None if no clear intent detected
    """
    # Skip very short messages
    if len(text.strip()) < MIN_MESSAGE_LENGTH:
        return None
    
    # Try fast rules first
    result = RulesEngine.classify(text)
    if result:
        logger.info(f"Intent detected by rules: {result.intent_type.value} (confidence: {result.confidence:.2f})")
        return result
    
    # Check if LLM is available
    if not settings.yandex_gpt_api_key and not settings.openai_api_key:
        return None
    
    # Fall back to LLM for ambiguous cases
    logger.info("Rules inconclusive, using LLM classification")
    result = await _classify_with_llm(text, context)
    
    if result:
        logger.info(f"Intent detected by LLM: {result.intent_type.value} (confidence: {result.confidence:.2f})")
    
    return result


def _build_confirmation_keyboard(intent_hash: str) -> InlineKeyboardMarkup:
    """Build confirmation keyboard for intent."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"intent:confirm:{intent_hash}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"intent:dismiss:{intent_hash}"),
        ]
    ])


async def intent_router_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main intent router handler for group messages.
    Analyzes message and routes to appropriate action handler.
    """
    if not update.message or not update.message.text:
        return
    
    message = update.message
    text = message.text
    user = update.effective_user
    chat = update.effective_chat
    
    # Skip if in active conversation
    if context.user_data.get("waiting_assignee_for") or \
       context.user_data.get("waiting_deadline_for") or \
       context.user_data.get("reminder_waiting_time"):
        return
    
    # Skip bot messages
    if message.from_user and message.from_user.is_bot:
        return
    
    # Skip @bot mentions (handled separately)
    if f"@{settings.bot_username}" in text.lower():
        return
    
    # Classify intent
    chat_context = "личное" if chat.type == "private" else "группа"
    intent_result = await classify_intent(text, chat_context)
    
    if not intent_result:
        # No clear intent detected
        return
    
    # Check if should ignore (confidence too low)
    if should_ignore(intent_result):
        logger.debug(f"Ignoring intent with low confidence: {intent_result.confidence:.2f}")
        return
    
    # Determine if needs confirmation
    if needs_confirmation(intent_result):
        # Show confirmation dialog
        intent_hash = _compute_intent_hash(text, chat.id)
        _store_pending_intent(
            context,
            intent_hash,
            intent_result,
            chat.id,
            user.id
        )
        
        confirmation_msg = format_confirmation_message(intent_result)
        keyboard = _build_confirmation_keyboard(intent_hash)
        
        await message.reply_text(
            confirmation_msg,
            reply_markup=keyboard
        )
        return
    
    # Auto-execute simple high-confidence actions
    from handlers.intent_executors import execute_intent
    await execute_intent(update, context, intent_result)


async def intent_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmation callbacks for pending intents."""
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split(":")
    if len(data_parts) != 3:
        return
    
    action = data_parts[1]  # confirm or dismiss
    intent_hash = data_parts[2]
    
    if action == "dismiss":
        _delete_pending_intent(context, intent_hash)
        await query.edit_message_text("👍 Окей, не буду")
        return
    
    if action == "confirm":
        # Get pending intent
        pending = _get_pending_intent(context, intent_hash)
        if not pending:
            await query.edit_message_text("⏰ Предложение устарело")
            return
        
        # Reconstruct intent result
        intent_type = IntentType(pending["intent_type"])
        intent_result = IntentResult(
            intent_type=intent_type,
            confidence=1.0,  # User confirmed
            extracted_data=pending["extracted_data"]
        )
        
        # Execute
        from handlers.intent_executors import execute_intent_from_callback
        await execute_intent_from_callback(query, context, intent_result, pending)
        
        _delete_pending_intent(context, intent_hash)

