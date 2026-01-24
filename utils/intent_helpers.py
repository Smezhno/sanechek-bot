"""Helper functions for intent classification and confidence logic."""
from typing import Dict, Any
from enum import Enum


class IntentType(Enum):
    """Types of user intents."""
    TASK = "task"
    REMINDER = "reminder"
    QUESTION = "question"
    EXPENSE = "expense"
    EDIT_TASK = "edit_task"
    EDIT_REMINDER = "edit_reminder"
    CLOSE_TASK = "close_task"
    NONE = "none"


class IntentResult:
    """Result of intent classification."""
    
    def __init__(
        self,
        intent_type: IntentType,
        confidence: float,
        extracted_data: Dict[str, Any],
        needs_confirmation: bool = False
    ):
        self.intent_type = intent_type
        self.confidence = confidence
        self.extracted_data = extracted_data
        self.needs_confirmation = needs_confirmation
    
    def __repr__(self):
        return (
            f"IntentResult(type={self.intent_type.value}, "
            f"confidence={self.confidence:.2f}, "
            f"needs_confirmation={self.needs_confirmation})"
        )


# Confidence thresholds
CONFIDENCE_THRESHOLDS = {
    "auto_execute": 0.85,  # Execute automatically
    "confirm": 0.65,       # Show confirmation
    "ignore": 0.65         # Below this - ignore
}


def is_simple_action(intent_result: IntentResult) -> bool:
    """
    Determine if action is simple enough to execute automatically.
    
    Simple actions:
    - Task without assignee and deadline
    - Reminder with explicit time
    - Question (always simple)
    """
    if intent_result.intent_type == IntentType.QUESTION:
        return True
    
    if intent_result.intent_type == IntentType.TASK:
        data = intent_result.extracted_data
        # Simple if no assignee and no deadline specified
        has_assignee = data.get("assignee") and data["assignee"] not in ["не указан", "нет", ""]
        has_deadline = data.get("deadline") and data["deadline"] not in ["не указан", "нет", ""]
        return not has_assignee and not has_deadline
    
    if intent_result.intent_type == IntentType.REMINDER:
        data = intent_result.extracted_data
        # Simple if time is specified
        has_time = data.get("reminder_time") and data["reminder_time"] not in ["не указан", "нет", ""]
        return has_time
    
    return False


def needs_confirmation(intent_result: IntentResult) -> bool:
    """
    Determine if intent needs confirmation before execution.
    
    Needs confirmation if:
    - Confidence < 0.85
    - Complex action (has assignee, recurring task, etc.)
    """
    # Low confidence always needs confirmation
    if intent_result.confidence < CONFIDENCE_THRESHOLDS["auto_execute"]:
        return True
    
    # Complex actions need confirmation even with high confidence
    if not is_simple_action(intent_result):
        return True
    
    return False


def should_ignore(intent_result: IntentResult) -> bool:
    """Determine if intent should be ignored (confidence too low)."""
    return intent_result.confidence < CONFIDENCE_THRESHOLDS["ignore"]


def format_confirmation_message(intent_result: IntentResult) -> str:
    """Format confirmation message for user."""
    if intent_result.intent_type == IntentType.TASK:
        task_text = intent_result.extracted_data.get("task_text", "")
        assignee = intent_result.extracted_data.get("assignee")
        deadline = intent_result.extracted_data.get("deadline")
        
        msg = f'Кажется, ты хочешь создать задачу:\n"{task_text}"\n'
        
        if assignee and assignee not in ["не указан", "нет", ""]:
            msg += f"Исполнитель: {assignee}\n"
        if deadline and deadline not in ["не указан", "нет", ""]:
            msg += f"Дедлайн: {deadline}\n"
        
        msg += "\nСоздать?"
        return msg
    
    elif intent_result.intent_type == IntentType.REMINDER:
        text = intent_result.extracted_data.get("reminder_text", "")
        time = intent_result.extracted_data.get("reminder_time", "")
        
        msg = f'Кажется, ты хочешь напоминание:\n"{text}"\n'
        if time:
            msg += f"Время: {time}\n"
        msg += "\nСоздать?"
        return msg
    
    elif intent_result.intent_type == IntentType.QUESTION:
        question = intent_result.extracted_data.get("question", "")
        return f'Отвечаю на вопрос: "{question[:50]}..."'
    
    return "Выполнить действие?"

