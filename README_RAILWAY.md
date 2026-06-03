# Запуск Telegram-бота на Railway

## 1. Что загрузить в GitHub
Загрузите в репозиторий все файлы из этого архива:

- `bot.py`
- `config.py`
- `database.py`
- `tasks.py`
- `utils.py`
- `requirements.txt`
- `Procfile`
- `.gitignore`

## 2. Railway Variables
В Railway откройте проект → ваш сервис → **Variables** и добавьте:

```text
TELEGRAM_TOKEN=токен_бота_из_BotFather
DEEPSEEK_API_KEY=ключ_DeepSeek
OPENAI_API_KEY=ключ_OpenAI
ADMIN_IDS=ваш_telegram_id
DB_FILE=/data/bot_data.db
```

Если администраторов несколько:

```text
ADMIN_IDS=123456789,987654321
```

## 3. Railway Volume
Чтобы бот не забывал пользователей, задачи и историю после redeploy:

1. Откройте сервис бота в Railway.
2. Найдите раздел **Volumes**.
3. Нажмите **Add Volume**.
4. В поле **Mount path** укажите:

```text
/data
```

5. В Variables оставьте:

```text
DB_FILE=/data/bot_data.db
```

Это не папка на вашем компьютере. Это постоянная папка внутри Railway.

## 4. Проверка после деплоя
После запуска напишите боту в Telegram:

```text
/start
```

Затем проверьте команды:

```text
/help
/get_contact_info
/new_task
/my_tasks
```

## 5. Что исправлено в этом архиве

- исправлен вызов OpenAI Whisper под `openai>=1.0.0`;
- голосовое сообщение теперь распознается и сразу отправляется в DeepSeek;
- исправлена работа дат SQLite для дедлайнов задач;
- добавлена колонка `updated_at` и миграция старой базы;
- добавлена поддержка Railway Volume через переменную `DB_FILE`;
- исправлена зависимость `python-telegram-bot[job-queue]` для фонового планировщика;
- добавлены недостающие команды `/task_info` и `/assign_task`;
- убран BOM/мусорная кодировка из `Procfile` и `requirements.txt`;
- обычные сообщения к DeepSeek выполняются без блокировки Telegram-бота.
