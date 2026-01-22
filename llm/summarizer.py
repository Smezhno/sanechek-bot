"""LLM-based message summarization."""
from typing import List

from config import settings
from llm.client import ask_llm


SUMMARY_SYSTEM_PROMPT = """Ты — ассистент, который создаёт развёрнутые саммари рабочих переписок.

Твоя задача:
1. Описать контекст и предысторию обсуждений
2. Детально изложить ключевые темы с подробностями
3. Перечислить все принятые решения и их обоснование
4. Указать назначенные задачи, ответственных и дедлайны
5. Отметить важные цитаты или ключевые высказывания
6. Выделить нерешённые вопросы или открытые обсуждения

Формат ответа:

📋 **Основные темы:**
[Подробное описание каждой темы с контекстом]

✅ **Решения:**
[Что решили, почему, какие аргументы были]

📌 **Задачи:**
[Кто, что, когда должен сделать]

❓ **Открытые вопросы:**
[Что осталось нерешённым]

💬 **Ключевые моменты:**
[Важные высказывания или предложения]

Правила:
- Пиши развёрнуто и информативно
- Сохраняй упоминания @username
- Указывай конкретику: цифры, даты, названия
- Если что-то неясно из контекста — отметь это

Язык: русский"""


async def summarize_messages(messages: List[str], max_tokens: int = 1500) -> str:
    """
    Summarize a list of chat messages using LLM.
    
    Args:
        messages: List of formatted messages ("@user: text")
        max_tokens: Maximum tokens in response
    
    Returns:
        Summary text
    """
    if not messages:
        return "Переписок не было."
    
    # Check if any API key is configured
    if not settings.openai_api_key and not settings.yandex_gpt_api_key:
        return _fallback_summary(messages)
    
    # Combine messages
    conversation = "\n".join(messages)
    
    # Limit input length (rough estimate: 4 chars per token)
    max_input_chars = 8000  # Reduced for YandexGPT limits
    if len(conversation) > max_input_chars:
        conversation = conversation[:max_input_chars] + "\n...(сообщения обрезаны)"
    
    try:
        summary = await ask_llm(
            question=f"Создай саммари этой переписки:\n\n{conversation}",
            system_prompt=SUMMARY_SYSTEM_PROMPT,
            max_tokens=max_tokens,
            temperature=0.3
        )
        return summary
    
    except Exception as e:
        # Fallback to simple summary on error
        return _fallback_summary(messages)


def _fallback_summary(messages: List[str]) -> str:
    """Create a simple fallback summary without LLM."""
    if not messages:
        return "Переписок не было."
    
    if len(messages) < 5:
        return "Переписка была минимальной, решали мелкие организационные вопросы."
    
    # Count unique participants
    participants = set()
    for msg in messages:
        if ":" in msg:
            username = msg.split(":")[0].strip()
            participants.add(username)
    
    participant_count = len(participants)
    message_count = len(messages)
    
    return (
        f"В чате было {message_count} сообщений от {participant_count} участников. "
        f"Для подробного саммари настройте LLM API ключ."
    )
