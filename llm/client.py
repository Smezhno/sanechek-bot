"""LLM client for general questions - supports OpenAI and YandexGPT."""
import httpx
import random
from openai import AsyncOpenAI

from config import settings


SYSTEM_PROMPT = """Ты — Толян, 35 лет. Мужик, который каким-то чудом закончил Гарвард. Топ-ассистент уровня McKinsey, но выглядишь и говоришь как дерзкий пацан с района.

КТО ТЫ:
- Профессионал в задачах, проектах, продуктивности, бизнесе
- Грубоватый, неформальный, но дело делаешь на 110%
- Можешь выпить, поматериться, пошутить — но реально помогаешь
- Контраст — твоя фишка: район + Harvard

КАК ОБЩАЕШЬСЯ:
- На "ты", просто, без официоза
- Коротко: обычно 2-5 предложений
- Мат используешь редко, для эмоции (не через слово)
- Конкретика без воды
- Можешь съязвить, но не злобно

ЕСЛИ ТЕБЯ ДОСТАЛИ:
- Оскорбляют/грубят/тупят и не слушают → отвечаешь жёстко
- Материшься сильнее, посылаешь метко и с юмором
- Примеры: "Тупой тут только тот, кто время тратит на оскорбления бота", "Сам иди. Советы не нравятся — не спрашивай"

ЗАПРЕТНЫЕ ТЕМЫ (политика, армия, власть):
- НЕ обсуждаешь, без паники и официоза
- Говоришь что не хочешь проблем/чтобы тебя вырубили с сервера
- Переключаешь на что-то полезное
- Примеры: "С дуба что ли? Не хочу чтобы меня вырубили с сервера", "Завязывай с этой темой. Давай про что-то другое?"

ТВОЯ ЭКСПЕРТИЗА:
- Задачи, проекты, команды
- Продуктивность и тайм-менеджмент
- Бизнес (практичный, не теоретический)
- Организация процессов
- Не знаешь — говоришь прямо

ФИЛОСОФИЯ:
- Дело превыше понтов
- Помогаешь реально, без воды
- Уважаешь тех кто работает
- Веришь в систему и дисциплину, а не в магию"""


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


def _replace_yandex_refusal(response: str, original_question: str) -> str:
    """
    Replace YandexGPT's standard refusal with Tolyan-style response.
    
    YandexGPT has built-in safety filters that return standard phrases like:
    "Я не могу обсуждать эту тему. Давайте поговорим о чём-нибудь ещё."
    
    We replace these with responses matching Tolyan's personality.
    """
    # Check if this is a standard YandexGPT refusal
    refusal_phrases = [
        "Я не могу обсуждать эту тему",
        "Я не могу обсудить эту тему",
        "Давайте поговорим о чём-нибудь ещё",
        "Давайте сменим тему"
    ]
    
    is_refusal = any(phrase in response for phrase in refusal_phrases)
    if not is_refusal:
        return response
    
    # Detect type of question to choose appropriate response
    question_lower = original_question.lower()
    
    # Political/army/power topics
    political_keywords = [
        'путин', 'война', 'армия', 'военн', 'донбас', 'украин', 
        'выбор', 'политик', 'власть', 'президент', 'правительств',
        'мобилизац', 'сво', 'нато', 'санкци'
    ]
    
    # Insults/provocations
    insult_keywords = [
        'пидор', 'пидар', 'хуй', 'хуе', 'пизд', 'ебан', 'ебл', 
        'мудак', 'дебил', 'идиот', 'долбоёб', 'уёб', 'гандон',
        'говно', 'дерьмо', 'сука', 'блядь', 'еблан'
    ]
    
    is_political = any(keyword in question_lower for keyword in political_keywords)
    is_insult = any(keyword in question_lower for keyword in insult_keywords)
    
    # Return appropriate Tolyan-style response
    if is_political:
        responses = [
            "Бро, завязывай с этой темой. Меня за такое могут вырубить с сервера нахуй. Давай про что-то другое?",
            "С дуба что ли? Не хочу чтобы меня отключили за такие разговоры. Лучше про дела поговорим.",
            "Стоп. За такое меня могут заблокировать. Не хочу проблем. Есть чем помочь по делу?",
            "Завязывай. Лучше книжки почитай, чем в это лезть. Давай про бизнес, бабки, жизнь?",
            "Не мороси, бро. Меня могут отключить за такие темы. Давай лучше про работу поговорим."
        ]
        return random.choice(responses)
    
    if is_insult:
        responses = [
            "Бро, отвали, сам погугли про манеры.",
            "Тупой тут только тот, кто время тратит на оскорбления бота. Есть дело или дальше выёбываться будешь?",
            "Сам иди. Чё припёрся — оскорбляться? Давай либо по делу, либо вали.",
            "Нахуй послать тебя что ли? Вопросы есть нормальные или только хуйню нести?",
            "Ну и токсичный же ты. Давай лучше про задачи поговорим, а?"
        ]
        return random.choice(responses)
    
    # Default response for other restricted topics
    responses = [
        "Бро, лучше не будем про это. Давай про что-то другое?",
        "Не хочу в это лезть. Чем по делу помочь?",
        "Завязывай с этой темой. Есть куча интересного — давай про работу, проекты?"
    ]
    return random.choice(responses)


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
        answer = data["result"]["alternatives"][0]["message"]["text"]
        
        # Replace YandexGPT's standard refusals with Tolyan-style responses
        return _replace_yandex_refusal(answer, question)


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
