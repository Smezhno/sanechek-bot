#!/bin/bash
# Setup script for Sanechek bot on Ubuntu 22.04

set -e

echo "=== Updating system ==="
sudo apt update && sudo apt upgrade -y

echo "=== Installing Python 3.11 ==="
sudo apt install -y python3.11 python3.11-venv python3-pip git

echo "=== Cloning repository ==="
cd ~
if [ -d "sanechek-bot" ]; then
    cd sanechek-bot && git pull
else
    git clone https://github.com/Smezhno/sanechek-bot.git
    cd sanechek-bot
fi

echo "=== Creating virtual environment ==="
python3.11 -m venv venv
source venv/bin/activate

echo "=== Installing dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Setup complete! ==="
echo "Now create .env file with your tokens:"
echo "  cd ~/sanechek-bot"
echo "  cp env.example .env"
echo "  nano .env"
echo ""
echo "Then run: sudo systemctl start sanechek-bot"

