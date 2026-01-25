"""Date and time parsing utilities for Russian natural language."""
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
import pytz
from dateutil import parser as dateutil_parser

from config import settings


class DateParseError(Exception):
    """Error raised when date parsing fails."""
    pass


def get_timezone():
    """Get configured timezone."""
    return pytz.timezone(settings.timezone)


def now_in_tz() -> datetime:
    """Get current time in configured timezone."""
    return datetime.now(get_timezone())


# Russian day names to weekday numbers (Monday = 0)
WEEKDAYS_RU = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среда": 2, "среду": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятница": 4, "пятницу": 4, "пт": 4,
    "суббота": 5, "субботу": 5, "сб": 5,
    "воскресенье": 6, "воскр": 6, "вс": 6,
}

# Russian month names
MONTHS_RU = {
    "января": 1, "январь": 1, "янв": 1,
    "февраля": 2, "февраль": 2, "фев": 2,
    "марта": 3, "март": 3, "мар": 3,
    "апреля": 4, "апрель": 4, "апр": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6, "июн": 6,
    "июля": 7, "июль": 7, "июл": 7,
    "августа": 8, "август": 8, "авг": 8,
    "сентября": 9, "сентябрь": 9, "сен": 9, "сент": 9,
    "октября": 10, "октябрь": 10, "окт": 10,
    "ноября": 11, "ноябрь": 11, "ноя": 11,
    "декабря": 12, "декабрь": 12, "дек": 12,
}

# Russian time of day
TIME_OF_DAY = {
    "утром": (9, 0),
    "утро": (9, 0),
    "днём": (12, 0),
    "днем": (12, 0),
    "день": (12, 0),
    "вечером": (18, 0),
    "вечер": (18, 0),
    "ночью": (23, 0),
    "ночь": (23, 0),
}

# Russian number words
NUMBER_WORDS = {
    "один": 1, "одну": 1, "одного": 1,
    "два": 2, "две": 2, "двух": 2,
    "три": 3, "трёх": 3, "трех": 3,
    "четыре": 4, "четырёх": 4, "четырех": 4,
    "пять": 5, "пяти": 5,
    "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9,
    "десять": 10, "десяти": 10,
    "полчаса": 30,  # special case for minutes
}


def _extract_time(text: str) -> Tuple[Optional[int], Optional[int], str]:
    """Extract time (hour, minute) from text. Returns remaining text."""
    text_lower = text.lower().strip()

    # Pattern 1: "в 15:30" or "в 15.30" or "в 15"
    time_pattern_with_v = r"в\s+(\d{1,2})(?:[:.](\d{2}))?"
    match = re.search(time_pattern_with_v, text_lower)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            remaining = text_lower[:match.start()] + text_lower[match.end():]
            return hour, minute, remaining.strip()

    # Pattern 2: "15:30" or "15.30" without "в " prefix (for /edit command)
    time_pattern_bare = r"^(\d{1,2})[:.](\d{2})$"
    match = re.match(time_pattern_bare, text_lower)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute, ""

    # Check for time of day words
    for word, (hour, minute) in TIME_OF_DAY.items():
        if word in text_lower:
            remaining = text_lower.replace(word, "").strip()
            return hour, minute, remaining

    return None, None, text_lower


def _parse_relative_time(text: str) -> Optional[datetime]:
    """Parse relative time expressions like 'через 30 минут'."""
    text_lower = text.lower().strip()
    now = now_in_tz()
    
    # "через X минут/часов/дней"
    patterns = [
        (r"через\s+(\d+)\s+минут", "minutes"),
        (r"через\s+(\d+)\s+мин", "minutes"),
        (r"через\s+минуту", "one_minute"),
        (r"через\s+минутку", "one_minute"),
        (r"через\s+(\d+)\s+час", "hours"),
        (r"через\s+час\b", "one_hour"),
        (r"через\s+часик", "one_hour"),
        (r"через\s+(\d+)\s+дн", "days"),
        (r"через\s+(\d+)\s+день", "days"),
        (r"через\s+(\d+)\s+дней", "days"),
        (r"через\s+день", "one_day"),
        (r"через\s+полчаса", "half_hour"),
    ]
    
    for pattern, unit in patterns:
        match = re.search(pattern, text_lower)
        if match:
            if unit == "half_hour":
                return now + timedelta(minutes=30)
            elif unit == "one_minute":
                return now + timedelta(minutes=1)
            elif unit == "one_hour":
                return now + timedelta(hours=1)
            elif unit == "one_day":
                return (now + timedelta(days=1)).replace(hour=0, minute=1)
            value = int(match.group(1))
            if unit == "minutes":
                return now + timedelta(minutes=value)
            elif unit == "hours":
                return now + timedelta(hours=value)
            elif unit == "days":
                return (now + timedelta(days=value)).replace(hour=0, minute=1)
    
    # Check for word numbers: "через два часа"
    for word, value in NUMBER_WORDS.items():
        if word == "полчаса":
            continue  # handled above
        
        patterns_word = [
            (rf"через\s+{word}\s+минут", "minutes", value),
            (rf"через\s+{word}\s+час", "hours", value),
            (rf"через\s+{word}\s+дн", "days", value),
        ]
        
        for pattern, unit, val in patterns_word:
            if re.search(pattern, text_lower):
                if unit == "minutes":
                    return now + timedelta(minutes=val)
                elif unit == "hours":
                    return now + timedelta(hours=val)
                elif unit == "days":
                    return (now + timedelta(days=val)).replace(hour=0, minute=1)
    
    return None


def _parse_weekday(text: str) -> Optional[datetime]:
    """Parse weekday expressions like 'в пятницу'."""
    text_lower = text.lower().strip()
    now = now_in_tz()
    
    # Extract time first
    hour, minute, remaining = _extract_time(text_lower)
    if hour is None:
        hour, minute = 0, 1  # Default: 00:01
    
    for day_name, target_weekday in WEEKDAYS_RU.items():
        if day_name in remaining:
            current_weekday = now.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:
                days_ahead += 7  # Next week
            
            target_date = now + timedelta(days=days_ahead)
            return target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    return None


def _parse_date_expression(text: str) -> Optional[datetime]:
    """Parse date expressions like 'завтра', '15 января', '15.01'."""
    text_lower = text.lower().strip()
    now = now_in_tz()
    
    # Extract time first
    hour, minute, remaining = _extract_time(text_lower)
    
    # Default time for deadlines is 00:01, for reminders it's 12:00
    default_hour = 0 if hour is None else hour
    default_minute = 1 if minute is None else minute
    
    # If only time specified (no date keywords), use today if not passed, else tomorrow
    if hour is not None and not remaining.strip():
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            # Time has passed today, use tomorrow
            target = target + timedelta(days=1)
        return target
    
    # "сегодня"
    if "сегодня" in remaining:
        return now.replace(hour=hour or 12, minute=minute or 0, second=0, microsecond=0)
    
    # "завтра"
    if "завтра" in remaining:
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=default_hour, minute=default_minute, second=0, microsecond=0)
    
    # "послезавтра"
    if "послезавтра" in remaining:
        day_after = now + timedelta(days=2)
        return day_after.replace(hour=default_hour, minute=default_minute, second=0, microsecond=0)
    
    # "15 января" or "15 янв"
    for month_name, month_num in MONTHS_RU.items():
        pattern = rf"(\d{1,2})\s+{month_name}"
        match = re.search(pattern, remaining)
        if match:
            day = int(match.group(1))
            try:
                year = now.year
                target = now.replace(month=month_num, day=day, hour=default_hour, 
                                    minute=default_minute, second=0, microsecond=0)
                if target < now:
                    target = target.replace(year=year + 1)
                return target
            except ValueError:
                raise DateParseError("Некорректная дата")
    
    # "15.01" or "15.01.26" or "15.01.2026"
    date_patterns = [
        r"(\d{1,2})\.(\d{1,2})\.(\d{4})",  # 15.01.2026
        r"(\d{1,2})\.(\d{1,2})\.(\d{2})",  # 15.01.26
        r"(\d{1,2})\.(\d{1,2})",           # 15.01
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, remaining)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            year = now.year
            
            if len(match.groups()) == 3:
                year_str = match.group(3)
                if len(year_str) == 2:
                    year = 2000 + int(year_str)
                else:
                    year = int(year_str)
            
            try:
                target = now.replace(year=year, month=month, day=day, 
                                    hour=default_hour, minute=default_minute, 
                                    second=0, microsecond=0)
                # If no year specified and date is in past, move to next year
                if len(match.groups()) == 2 and target < now:
                    target = target.replace(year=year + 1)
                return target
            except ValueError:
                raise DateParseError("Некорректная дата")
    
    return None


def parse_deadline(text: str) -> datetime:
    """
    Parse deadline from Russian natural language.
    
    Examples:
        - "завтра" -> next day at 00:01
        - "в пятницу" -> next Friday at 00:01
        - "15.02" -> Feb 15 at 00:01
        - "через 3 дня" -> current date + 3 days at 00:01
    
    Raises:
        DateParseError: If date cannot be parsed or is in the past
    """
    text = text.strip()
    if not text:
        raise DateParseError("Не указана дата")
    
    now = now_in_tz()
    result = None
    
    # Try relative time first
    result = _parse_relative_time(text)
    
    # Try weekday
    if result is None:
        result = _parse_weekday(text)
    
    # Try date expression
    if result is None:
        result = _parse_date_expression(text)
    
    if result is None:
        raise DateParseError("Не понял дату. Попробуй: завтра, в пятницу, 15.02")
    
    # Check if in past
    if result <= now:
        raise DateParseError("Эта дата уже прошла. Укажи дату в будущем")
    
    return result


def parse_reminder_time(text: str) -> datetime:
    """
    Parse reminder time from Russian natural language.
    Similar to parse_deadline but with different defaults (12:00 instead of 00:01).
    
    Examples:
        - "через 30 минут" -> now + 30 minutes
        - "завтра в 15:00" -> tomorrow at 15:00
        - "в пятницу" -> next Friday at 12:00
    
    Raises:
        DateParseError: If time cannot be parsed or is in the past
    """
    text = text.strip()
    if not text:
        raise DateParseError("Не указано время")
    
    now = now_in_tz()
    result = None
    
    # Try relative time first
    result = _parse_relative_time(text)
    
    # Try weekday
    if result is None:
        result = _parse_weekday(text)
        # Default to 12:00 for reminders if no time specified
        if result and result.hour == 0 and result.minute == 1:
            result = result.replace(hour=12, minute=0)
    
    # Try date expression with special handling for time of day
    if result is None:
        hour, minute, remaining = _extract_time(text)
        
        # Handle "утром", "вечером" etc.
        for word, (h, m) in TIME_OF_DAY.items():
            if word in text.lower():
                base_date = now
                # Check if time has passed today
                target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if target_time <= now:
                    base_date = now + timedelta(days=1)
                result = base_date.replace(hour=h, minute=m, second=0, microsecond=0)
                break
        
        if result is None:
            result = _parse_date_expression(text)
            # Default to 12:00 for reminders if no time specified
            if result and hour is None:
                result = result.replace(hour=12, minute=0)
    
    if result is None:
        raise DateParseError(
            "Не понял, когда напомнить. Укажи время, например: "
            "\"через 30 минут\", \"завтра в 15:00\", \"в пятницу\""
        )
    
    # Check if in past
    if result <= now:
        raise DateParseError("Это время уже прошло. Укажи время в будущем")
    
    # Check minimum interval (1 minute)
    if (result - now).total_seconds() < 60:
        raise DateParseError("Минимальный интервал — 1 минута")
    
    # Check maximum interval (3 months)
    max_delta = timedelta(days=settings.max_reminder_months * 30)
    if result - now > max_delta:
        raise DateParseError("Максимальный интервал — 3 месяца")
    
    return result

