"""Expense categorization utilities."""
import re
from typing import Optional


# Category keywords mapping
CATEGORY_KEYWORDS = {
    "Транспорт": [
        "такси", "uber", "яндекс.такси", "яндекс такси", "ситимобил",
        "метро", "автобус", "троллейбус", "трамвай", "маршрутка",
        "бензин", "заправка", "топливо", "азс",
        "парковка", "стоянка",
        "поезд", "электричка", "жд", "ржд",
        "самолёт", "самолет", "авиа", "билет",
        "каршеринг", "аренда авто", "прокат",
    ],
    "Еда": [
        "обед", "завтрак", "ужин", "еда", "перекус",
        "кофе", "чай", "напитки",
        "ресторан", "кафе", "столовая", "буфет",
        "доставка еды", "delivery", "яндекс.еда", "яндекс еда",
        "деливери клаб", "delivery club",
        "продукты", "магазин", "супермаркет",
        "пицца", "суши", "роллы", "бургер", "фастфуд",
    ],
    "Канцелярия": [
        "бумага", "ручка", "ручки", "карандаш", "карандаши",
        "блокнот", "тетрадь", "записная книжка",
        "степлер", "скрепки", "кнопки", "скотч",
        "папка", "файл", "файлы", "конверт", "конверты",
        "расходники", "канцтовары", "канцелярия",
        "картридж", "тонер", "чернила",
        "маркер", "маркеры", "фломастер", "фломастеры",
        "ножницы", "линейка", "ластик",
    ],
    "Оборудование": [
        "компьютер", "ноутбук", "монитор", "клавиатура", "мышь", "мышка",
        "принтер", "сканер", "мфу",
        "телефон", "смартфон", "планшет",
        "наушники", "гарнитура", "веб-камера", "вебкамера",
        "кабель", "провод", "зарядка", "зарядное",
        "флешка", "usb", "жёсткий диск", "жесткий диск", "ssd", "hdd",
        "роутер", "маршрутизатор", "модем",
        "мебель", "стол", "стул", "кресло", "шкаф",
        "лампа", "светильник", "освещение",
    ],
    "Связь": [
        "телефон", "мобильный", "сотовый", "симка", "sim",
        "интернет", "wifi", "вай-фай", "вайфай",
        "тариф", "абонентская плата", "связь",
        "мтс", "билайн", "мегафон", "теле2", "yota",
        "роуминг",
    ],
    "Подписки": [
        "подписка", "subscription",
        "netflix", "spotify", "apple music",
        "облако", "cloud", "яндекс.диск", "google drive", "dropbox",
        "zoom", "slack", "notion", "figma",
        "хостинг", "домен", "сервер", "vps",
    ],
    "Услуги": [
        "услуга", "сервис", "обслуживание",
        "ремонт", "починка",
        "уборка", "клининг",
        "доставка", "курьер",
        "консультация", "юрист", "бухгалтер",
        "перевод", "нотариус",
    ],
}


def categorize_expense(description: str) -> str:
    """
    Automatically categorize expense based on description.
    
    Args:
        description: Expense description text
    
    Returns:
        Category name or "Прочее" if no match found
    """
    description_lower = description.lower()
    
    # Check each category's keywords
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            # Use word boundary matching for short keywords
            if len(keyword) <= 3:
                pattern = rf"\b{re.escape(keyword)}\b"
                if re.search(pattern, description_lower):
                    return category
            else:
                if keyword in description_lower:
                    return category
    
    return "Прочее"


def get_all_categories() -> list[str]:
    """Get list of all available categories."""
    return list(CATEGORY_KEYWORDS.keys()) + ["Прочее"]

