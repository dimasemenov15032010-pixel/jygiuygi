# Установка бота на сервер

## 1. Требования
- Python 3.11+
- Ubuntu / Debian

## 2. Установка

```bash
# Создать папку и скопировать файлы
sudo mkdir -p /opt/bot
sudo cp bot.py /opt/bot/
sudo cp .env /opt/bot/

# Создать виртуальное окружение
python3 -m venv /opt/bot/venv
/opt/bot/venv/bin/pip install -r requirements.txt

# Создать пользователя для запуска
sudo useradd -r -s /bin/false bot
sudo chown -R bot:bot /opt/bot
```

## 3. Настройка

Скопировать `.env.example` в `.env` и заполнить:

```bash
cp .env.example .env
nano .env
```

Обязательно заполнить:
- `BOT_TOKEN` — токен от @BotFather
- `MAIN_GROUP_ID` — ID группы где работает /panel и стата бота

## 4. Запуск через systemd

```bash
sudo cp bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bot
sudo systemctl start bot

# Проверить статус
sudo systemctl status bot

# Смотреть логи
sudo journalctl -u bot -f
```

## 5. Обновление бота

```bash
sudo cp bot.py /opt/bot/
sudo systemctl restart bot
```
