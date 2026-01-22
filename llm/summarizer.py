"""LLM-based message summarization."""
from typing import List
from openai import AsyncOpenAI

from config import settings


# Initialize OpenAI client
client = None

def get_client() -> AsyncOpenAI:
    """Get or create OpenAI client."""
    global client
    if client is None:
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url
        )
    return client


SUMMARY_SYSTEM_PROMPT = """Ты — ассистент, который создаёт краткие саммари рабочих переписок.

Твоя задача:
1. Выделить ключевые обсуждаемые темы
2. Отметить принятые решения
3. Указать назначенные задачи и ответственных
4. Сохранить важные детали и контекст

Формат:
- Пиши кратко и по делу
- Используй нейтральный тон
- Сохраняй упоминания пользователей (@username)
- Не добавляй информацию, которой нет в переписке
- Если переписка минимальная или неинформативная, напиши об этом кратко

Язык: русский"""


async def summarize_messages(messages: List[str], max_tokens: int = 500) -> str:
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
    
    # Check if API key is configured
    if not settings.openai_api_key:
        return _fallback_summary(messages)
    
    # Combine messages
    conversation = "\n".join(messages)
    
    # Limit input length (rough estimate: 4 chars per token)
    max_input_chars = 12000  # ~3000 tokens for input
    if len(conversation) > max_input_chars:
        conversation = conversation[:max_input_chars] + "\n...(сообщения обрезаны)"
    
    try:
        client = get_client()
        
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": f"Создай саммари этой переписки:\n\n{conversation}"}
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        
        return response.choices[0].message.content.strip()
    
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
        f"Для подробного саммари настройте OpenAI API ключ."
    )

