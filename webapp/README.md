# Санечек Mini App

Веб-интерфейс для Telegram бота Санечек, созданный на основе Telegram Mini Apps UI Kit.

## Структура

```
webapp/
├── index.html      # Основной HTML файл
├── styles.css      # Стили (Telegram Design System)
├── app.js          # JavaScript логика и Telegram Web Apps API
├── api.py          # FastAPI endpoints для взаимодействия с ботом
└── run_server.py   # Скрипт запуска веб-сервера
```

## Запуск

### Локально

```bash
# Установить зависимости
pip install -r ../requirements.txt

# Запустить веб-сервер
python webapp/run_server.py
```

Сервер будет доступен по адресу: `http://localhost:8000`

### В продакшене

1. Настроить веб-сервер (nginx) для проксирования запросов
2. Настроить SSL сертификат
3. Добавить URL в настройки бота через [@BotFather](https://t.me/BotFather)

## Настройка бота

1. Откройте [@BotFather](https://t.me/BotFather)
2. Выберите вашего бота
3. Отправьте команду `/newapp`
4. Укажите название и описание приложения
5. Загрузите иконку (512x512px)
6. Укажите URL вашего веб-приложения (например: `https://yourdomain.com`)

## API Endpoints

### Tasks
- `GET /api/tasks` - Получить список задач пользователя
- `POST /api/tasks` - Создать новую задачу
- `POST /api/tasks/{id}/close` - Закрыть задачу

### Expenses
- `GET /api/expenses` - Получить список расходов
- `POST /api/expenses` - Добавить расход

### Reminders
- `GET /api/reminders` - Получить список напоминаний
- `POST /api/reminders` - Создать напоминание

### Summary
- `GET /api/summary` - Получить саммари чата

## Аутентификация

Все API endpoints требуют заголовок `X-Telegram-Init-Data` с данными инициализации Telegram Web App. Данные проверяются на подлинность с использованием секретного ключа бота.

## Дизайн

Приложение использует Telegram Design System:
- Цвета берутся из `tg.themeParams`
- Стили соответствуют нативному виду Telegram
- Поддержка темной темы через Telegram Web Apps API

## Разработка

Для разработки можно использовать локальный сервер с CORS, который уже настроен в `api.py`.

Для тестирования без Telegram можно временно отключить проверку аутентификации в `api.py`.

