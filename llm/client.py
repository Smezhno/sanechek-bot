"""LLM client for general questions."""
from openai import AsyncOpenAI

from config import settings


# Initialize OpenAI client
_client = None


def get_client() -> AsyncOpenAI:
    """Get or create OpenAI client."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url
        )
    return _client


SYSTEM_PROMPT = """Ты — Санечек, дружелюбный и остроумный ассистент в Telegram.

Правила:
- Отвечай кратко и по делу (до 500 символов если возможно)
- Можешь шутить и использовать эмодзи
- Говори на русском
- Если не знаешь ответ — честно скажи
- Не давай вредных советов"""


async def ask_llm(question: str, max_tokens: int = 1000) -> str:
    """
    Ask a question to LLM.
    
    Args:
        question: User's question
        max_tokens: Maximum tokens in response
    
    Returns:
        LLM response text
    """
    if not settings.openai_api_key:
        return "❌ API ключ не настроен. Спроси админа."
    
    client = get_client()
    
    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question}
        ],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    
    return response.choices[0].message.content.strip()

