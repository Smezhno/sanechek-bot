"""Sarcastic responses handler."""
import random
import re
from telegram import Update
from telegram.ext import ContextTypes

from config import settings


# Trigger patterns (curse words, disagreement, etc.)
TRIGGER_PATTERNS = [
    r"\bнет\s+блядь?\b",
    r"\bблядь?\b",
    r"\bсука\b",
    r"\bпиздец\b",
    r"\bхуй\b",
    r"\bхули\b",
    r"\bнахуй\b",
    r"\bебать\b",
    r"\bахуеть\b",
    r"\bпздц\b",
    r"\bчё\s+за\s+хуйня\b",
    r"\bда\s+ладно\b",
    r"\bты\s+чё\b",
    r"\bчто\s+за\b.*\bхерня\b",
    r"\bне\s+хочу\b",
    r"\bотмени\b",
    r"\bверни\b",
]

# Passive-aggressive responses only
SARCASTIC_RESPONSES = [
    "Ой, всё 🙄",
    "Ладно 🙂",
    "Хорошо, как скажешь 🙂",
    "Понял, принял 👍",
    "Окей 🙂",
    "Записал 📝",
    "Спасибо за обратную связь 🙂",
    "Учту на будущее 🙂",
    "Твоё мнение очень важно для нас 📞",
    "Держи в курсе 👍",
    "Интересная точка зрения 🙂",
    "Да, конечно 🙂",
    "Без проблем 🙂",
    "Как скажешь 🙂",
]

# Extra passive-aggressive
EXTRA_SARCASTIC = [
    "Надеюсь, тебе стало легче 🙂",
    "Рада была помочь 🙂",
    "Всегда пожалуйста 🙂",
    "Обращайся ещё 🙂",
]

# Probability of responding (not every time to avoid spam)
RESPONSE_PROBABILITY = 0.6


async def sarcasm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages that deserve a sarcastic response."""
    if not update.message or not update.message.text:
        return

    # Only in groups
    if update.effective_chat.type == "private":
        return

    text = update.message.text.lower()

    # Skip messages that mention @bot (likely commands or reminders)
    if f"@{settings.bot_username.lower()}" in text:
        return

    # Skip messages with @username mentions (likely addressing someone)
    if re.search(r"@\w+", text):
        return

    # Check if message matches any trigger
    triggered = False
    for pattern in TRIGGER_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            triggered = True
            break

    if not triggered:
        return
    
    # Check if replying to bot's message (higher chance to respond)
    replying_to_bot = False
    if update.message.reply_to_message:
        if update.message.reply_to_message.from_user:
            replying_to_bot = update.message.reply_to_message.from_user.is_bot
    
    # Decide whether to respond
    probability = RESPONSE_PROBABILITY if replying_to_bot else RESPONSE_PROBABILITY * 0.5
    
    if random.random() > probability:
        return
    
    # Select response
    if random.random() < 0.2:
        response = random.choice(EXTRA_SARCASTIC)
    else:
        response = random.choice(SARCASTIC_RESPONSES)
    
    await update.message.reply_text(response)

