"""LLM client for general questions - supports OpenAI and YandexGPT."""
import httpx
from openai import AsyncOpenAI

from config import settings


SYSTEM_PROMPT = """Ты — Санечек, дружелюбный и остроумный ассистент в Telegram.

Правила:
- Отвечай кратко и по делу (до 500 символов если возможно)
- Можешь шутить и использовать эмодзи
- Говори на русском
- Если не знаешь ответ — честно скажи
- Не давай вредных советов"""


# OpenAI client cache
_openai_client = None


def get_openai_client() -> AsyncOpenAI:
    """Get or create OpenAI client."""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url if settings.openai_base_url else None
        )
    return _openai_client


async def ask_yandexgpt(
    question: str, 
    system_prompt: str = SYSTEM_PROMPT,
    max_tokens: int = 1000,
    temperature: float = 0.7
) -> str:
    """Ask YandexGPT."""
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    
    headers = {
        "Authorization": f"Api-Key {settings.yandex_gpt_api_key}",
        "Content-Type": "application/json",
    }
    
    # Use YandexGPT Lite for cost efficiency
    model_uri = f"gpt://{settings.yandex_folder_id}/yandexgpt-lite/latest"
    
    payload = {
        "modelUri": model_uri,
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens)
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": question}
        ]
    }
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        
        data = response.json()
        return data["result"]["alternatives"][0]["message"]["text"]


async def ask_openai(
    question: str,
    system_prompt: str = SYSTEM_PROMPT,
    max_tokens: int = 1000,
    temperature: float = 0.7
) -> str:
    """Ask OpenAI-compatible API."""
    client = get_openai_client()
    
    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question}
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    
    return response.choices[0].message.content.strip()


async def ask_llm(
    question: str, 
    system_prompt: str = SYSTEM_PROMPT,
    max_tokens: int = 1000,
    temperature: float = 0.7
) -> str:
    """
    Ask a question to LLM (auto-selects YandexGPT or OpenAI).
    
    Args:
        question: User's question
        system_prompt: System prompt for context
        max_tokens: Maximum tokens in response
        temperature: Creativity (0.0-1.0)
    
    Returns:
        LLM response text
    """
    # Prefer YandexGPT if configured
    if settings.yandex_gpt_api_key and settings.yandex_folder_id:
        return await ask_yandexgpt(question, system_prompt, max_tokens, temperature)
    
    # Fallback to OpenAI
    if settings.openai_api_key:
        return await ask_openai(question, system_prompt, max_tokens, temperature)
    
    return "❌ API ключ не настроен. Спроси админа."


# Legacy function for compatibility
def get_client() -> AsyncOpenAI:
    """Get OpenAI client (legacy, use ask_llm instead)."""
    return get_openai_client()
