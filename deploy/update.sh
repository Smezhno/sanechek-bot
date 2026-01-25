#!/bin/bash
# Скрипт для обновления продакшн сервера

set -e

echo "=== Обновление Sanechek Bot на продакшн ==="

# Переход в директорию проекта
cd /home/yc-user/sanechek-bot

# Обновление кода
echo "=== Получение последних изменений из git ==="
git pull

# Активация виртуального окружения и обновление зависимостей
echo "=== Обновление зависимостей ==="
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Перезапуск сервисов
echo "=== Перезапуск сервисов ==="
sudo systemctl restart sanechek-bot
sudo systemctl restart sanechek-webapp

# Проверка статуса
echo "=== Проверка статуса сервисов ==="
sudo systemctl status sanechek-bot --no-pager -l
echo ""
sudo systemctl status sanechek-webapp --no-pager -l

echo ""
echo "=== Обновление завершено! ==="

